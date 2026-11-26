"""Оркестрация конвейера: один прогон по профилю поиска.

    collect → normalize → dedup(store) → score → publish

Слои внедряются через конструктор (DI), поэтому каждый можно подменить/замокать
в тестах. Сейчас связывает заглушки Главы 0; реальные коллектор/скоринг/паблишер
встают на их места без изменения этого файла.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from .config import SearchProfile, Settings
from .errors import CollectionSkipped, CollectorBlockedError, DeliveryDeferred, DeliveryUncertain
from .interfaces import BaseCollector, Publisher, ScoringEngine, Storage
from .models import Listing, ScoredListing

logger = logging.getLogger(__name__)


@dataclass
class CollectionProgress:
    seen: int = 0
    new: int = 0
    changed: int = 0
    pages: int = 0
    pages_received: int = 0
    rejected: int = 0
    reason: str = "ok"
    retry_at: float | None = None


class Pipeline:
    def __init__(
        self,
        *,
        collectors: Sequence[BaseCollector],
        storage: Storage,
        scoring: ScoringEngine,
        publisher: Publisher,
        settings: Settings,
        post_delay: float = 3.0,
        max_per_cycle: int | None = None,
    ) -> None:
        self._collectors = list(collectors)
        self._storage = storage
        self._scoring = scoring
        self._publisher = publisher
        self._settings = settings
        # Пауза между публикациями — защита от флуд-лимитов Telegram.
        self._post_delay = post_delay
        # Лимит постов за прогон (топ-N лучших), None = без лимита.
        self._max_per_cycle = max_per_cycle

    async def collect_and_score(self, profile: SearchProfile) -> list[ScoredListing]:
        """Собрать новые лоты по профилю со ВСЕХ площадок, оценить, отдать топ-кандидатов.

        Публикацию НЕ делает — оркестратор объединит кандидатов со всех профилей
        и площадок и выберет глобальный топ (читаемый канал, а не спам).
        Блокировка одной площадки логируется и не мешает остальным.
        """
        candidates: list[ScoredListing] = []
        for collector in self._collectors:
            fresh, _ = await self.collect_source(collector, profile)
            for listing in fresh:
                score = await self._scoring.score(listing, profile)
                if not score.is_publishable:
                    logger.debug("rejected %s: %s", listing.url, score.reject_reason)
                    continue
                if score.total < profile.publish_threshold:
                    continue
                candidates.append(ScoredListing(listing=listing, score=score))
        return candidates

    async def publish_top(
        self, candidates: list[ScoredListing], *, by_appeal: bool = False
    ) -> list[ScoredListing]:
        """Опубликовать глобальный топ кандидатов (со всех профилей).

        Сортируем по «вкусности» (или по красоте `appeal`, если by_appeal — для
        канала-витрины), схлопываем дубли по content_key (в прогоне и против
        истории), режем по лимиту цикла и постим с антифлуд-паузой.
        """
        if by_appeal:
            # Канал = витрина: вперёд самые красивые (WOW), при равенстве — вкуснее.
            ranked = sorted(
                candidates,
                key=lambda c: (c.listing.appeal or 0, c.score.total),
                reverse=True,
            )
        else:
            ranked = sorted(candidates, key=lambda c: c.score.total, reverse=True)

        published: list[ScoredListing] = []
        seen: set[str] = set()
        channel = self._settings.public_channel or "(public)"
        for scored in ranked:
            if self._max_per_cycle is not None and len(published) >= self._max_per_cycle:
                break
            content_key = scored.listing.content_key
            if content_key in seen:
                continue
            if await self._storage.was_posted(content_key):
                continue
            seen.add(content_key)
            if published and self._post_delay:
                await asyncio.sleep(self._post_delay)  # антифлуд между постами
            try:
                ok = await self._publisher.publish(scored, channel=channel)
            except DeliveryDeferred:
                break
            except DeliveryUncertain:
                logger.warning("Unconfirmed channel publication key=%s", content_key)
                continue
            if ok is None:
                continue
            if not ok:
                # Не отправилось — НЕ помечаем, попадёт в следующий цикл.
                logger.warning("пост не отправлен: %s", scored.listing.url)
                continue
            await self._storage.mark_posted(content_key, channel)
            published.append(scored)

        logger.info("published=%d (из %d кандидатов)", len(published), len(candidates))
        return published

    async def run_once(self, profile: SearchProfile) -> list[ScoredListing]:
        """Удобство для одного профиля: собрать+оценить и опубликовать топ."""
        candidates = await self.collect_and_score(profile)
        return await self.publish_top(candidates)

    async def collect_only(
        self, profile: SearchProfile, *, on_new: Callable[[], None] | None = None,
    ) -> tuple[int, dict[str, str]]:
        """Только собрать и сохранить (без скоринга/постинга) — для частого сбора,
        который питает и публичный канал, и персонального бота.

        Возвращает (число новых лотов, {source: 'ok'|'blocked'|'error'}) — статус по
        каждой площадке нужен health-мониторингу (алерт админам при падении). Сбой
        одной площадки не мешает остальным."""
        total = 0
        statuses = {}
        for collector in self._collectors:
            fresh, status = await self.collect_source(
                collector, profile, on_changes=(lambda _: on_new()) if on_new else None,
            )
            total += len(fresh)
            statuses[collector.source_name] = status
        return total, statuses

    async def collect_source(
        self, collector: BaseCollector, profile: SearchProfile,
        on_changes: Callable[[list[Listing]], None] | None = None,
    ) -> tuple[list[Listing], str]:
        return await collect_source(self._storage, collector, profile, on_changes)

    async def score_recent(
        self, profile: SearchProfile, since: datetime, *, min_score: float | None = None,
        listings: list[Listing] | None = None
    ) -> list[ScoredListing]:
        """Оценить свежие (по `since`) лоты города из БД — кандидаты для публикации.
        `min_score` — порог «вкусности» (None → publish_threshold профиля). Канал
        передаёт свой (низкий) порог: там отбор по красоте, а не по выгодности.
        Дедуп против истории постинга делает `publish_top`."""
        threshold = min_score if min_score is not None else profile.publish_threshold
        if listings is None:
            listings = await self._storage.recent_listings(profile.city, since)  # type: ignore[attr-defined]
        out: list[ScoredListing] = []
        for listing in listings:
            if not profile.accepts(listing):
                continue
            score = await self._scoring.score(listing, profile)
            if score.is_publishable and score.total >= threshold:
                out.append(ScoredListing(listing=listing, score=score))
        return out


async def collect_source(
    storage: Storage, collector: BaseCollector, profile: SearchProfile,
    on_changes: Callable[[list[Listing]], None] | None = None,
    *, progress: CollectionProgress | None = None,
) -> tuple[list[Listing], str]:
    fresh: list[Listing] = []
    saved = rejected = 0
    try:
        async for page in collector.fetch_pages(profile, limit=profile.collect_limit):
            if progress is not None:
                progress.pages_received += 1
            listings = []
            invalid_before = rejected
            for raw in page:
                try:
                    listing = collector.normalize(raw)
                except (
                    ValueError,
                    TypeError,
                    AttributeError,
                    KeyError,
                    IndexError,
                    OverflowError,
                ):
                    logger.warning(
                        "Invalid listing source=%s id=%s",
                        collector.source_name,
                        raw.external_id,
                    )
                    listing = None
                if listing is None:
                    rejected += 1
                else:
                    listings.append(listing)
            new, changed = await storage.upsert_changes(listings)
            fresh.extend(new)
            saved += len(listings)
            if progress is not None:
                progress.pages += 1
                progress.rejected += rejected - invalid_before
                progress.seen += len(listings)
                progress.new += len(new)
                progress.changed += len(changed) - len(new)
            if changed and on_changes:
                on_changes(changed)
    except CollectionSkipped as exc:
        if progress is not None:
            if str(exc) != "interval" or not progress.pages and progress.reason == "ok":
                progress.reason = {"interval": "interval", "cooldown": "cooldown",
                                   "shared proxy busy": "proxy_busy"}.get(str(exc), "skipped")
            if exc.retry_at is not None:
                progress.retry_at = max(progress.retry_at or exc.retry_at, exc.retry_at)
        return fresh, "partial" if saved else "skipped"
    except CollectorBlockedError as exc:
        if progress is not None:
            cause = str(exc)
            progress.reason = ("challenge" if "SmartCaptcha" in cause else
                               "schema" if "INITIAL_STATE" in cause else "source_blocked")
        logger.warning(
            "collect %s [%s]: блок (%s), saved=%d",
            profile.name,
            collector.source_name,
            exc,
            saved,
        )
        return fresh, "partial" if saved else "blocked"
    except Exception:
        if progress is not None:
            progress.reason = "collection_error"
        logger.exception("Collection failed source=%s saved=%d", collector.source_name, saved)
        return fresh, "partial" if saved else "error"
    logger.info("collect source=%s saved=%d new=%d rejected=%d",
                collector.source_name, saved, len(fresh), rejected)
    if rejected and progress is not None:
        progress.reason = "invalid_listing"
    return fresh, "partial" if rejected and saved else ("error" if rejected else "ok")
