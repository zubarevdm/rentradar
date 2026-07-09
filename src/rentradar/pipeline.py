"""Оркестрация конвейера: один прогон по профилю поиска.

    collect → normalize → dedup(store) → score → publish

Слои внедряются через конструктор (DI), поэтому каждый можно подменить/замокать
в тестах. Сейчас связывает заглушки Главы 0; реальные коллектор/скоринг/паблишер
встают на их места без изменения этого файла.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime

from .config import SearchProfile, Settings
from .errors import CollectorBlockedError
from .interfaces import BaseCollector, Publisher, ScoringEngine, Storage
from .models import ScoredListing

logger = logging.getLogger(__name__)


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
            try:
                raw_items = await collector.fetch(profile)
            except CollectorBlockedError as exc:
                logger.warning(
                    "profile=%s source=%s: блокировка (%s)",
                    profile.name,
                    collector.source_name,
                    exc,
                )
                continue
            listings = [n for r in raw_items if (n := collector.normalize(r))]
            fresh = await self._storage.upsert_listings(listings)
            logger.info(
                "profile=%s source=%s collected=%d new=%d",
                profile.name,
                collector.source_name,
                len(listings),
                len(fresh),
            )
            for listing in fresh:
                score = await self._scoring.score(listing, profile)
                if not score.is_publishable:
                    logger.debug("rejected %s: %s", listing.url, score.reject_reason)
                    continue
                if score.total < profile.publish_threshold:
                    continue
                candidates.append(ScoredListing(listing=listing, score=score))
        return candidates

    async def publish_top(self, candidates: list[ScoredListing]) -> list[ScoredListing]:
        """Опубликовать глобальный топ кандидатов (со всех профилей).

        Сортируем по «вкусности», схлопываем дубли по content_key (в прогоне и
        против истории), режем по лимиту цикла и постим с антифлуд-паузой.
        """
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
            ok = await self._publisher.publish(scored, channel=channel)
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

    async def collect_only(self, profile: SearchProfile) -> tuple[int, dict[str, str]]:
        """Только собрать и сохранить (без скоринга/постинга) — для частого сбора,
        который питает и публичный канал, и персонального бота.

        Возвращает (число новых лотов, {source: 'ok'|'blocked'|'error'}) — статус по
        каждой площадке нужен health-мониторингу (алерт админам при падении). Сбой
        одной площадки не мешает остальным."""
        new_total = 0
        statuses: dict[str, str] = {}
        for collector in self._collectors:
            try:
                raw_items = await collector.fetch(profile)
            except CollectorBlockedError as exc:
                logger.warning(
                    "collect %s [%s]: блок (%s)", profile.name, collector.source_name, exc
                )
                statuses[collector.source_name] = "blocked"
                continue
            except Exception:  # noqa: BLE001 — неожиданная ошибка площадки не валит сбор
                logger.exception(
                    "collect %s [%s]: ошибка", profile.name, collector.source_name
                )
                statuses[collector.source_name] = "error"
                continue
            listings = [n for r in raw_items if (n := collector.normalize(r))]
            fresh = await self._storage.upsert_listings(listings)
            new_total += len(fresh)
            statuses[collector.source_name] = "ok"
        return new_total, statuses

    async def score_recent(self, profile: SearchProfile, since: datetime) -> list[ScoredListing]:
        """Оценить свежие (по `since`) лоты города из БД — кандидаты для публикации.
        Дедуп против истории постинга делает `publish_top`."""
        listings = await self._storage.recent_listings(profile.city, since)  # type: ignore[attr-defined]
        out: list[ScoredListing] = []
        for listing in listings:
            score = await self._scoring.score(listing, profile)
            if score.is_publishable and score.total >= profile.publish_threshold:
                out.append(ScoredListing(listing=listing, score=score))
        return out
