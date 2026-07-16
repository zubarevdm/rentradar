"""Диспетчер персонального бота.

Раз в N минут (по интервалу каждого фильтра) подбирает новые лоты из уже
собранной БД под критерии пользователя и шлёт в личку. Площадки НЕ дёргаются —
матчинг идёт по `recent_listings`. Триал-гейт: первые `free_limit` отправок
бесплатно, дальше нужна активная подписка. Скам отсекается скорингом.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..config import SearchProfile, Settings
from ..interfaces import Publisher, ScoringEngine
from ..models import ScoredListing, dedupe_cross_source
from ..storage import SqlStorage
from .filters import UserFilter, matches
from .store import PersonalStore

logger = logging.getLogger(__name__)


class PersonalDispatcher:
    def __init__(
        self,
        *,
        store: PersonalStore,
        storage: SqlStorage,
        scoring: ScoringEngine,
        publisher: Publisher,
        settings: Settings,
    ) -> None:
        self._ps = store
        self._storage = storage
        self._scoring = scoring
        self._publisher = publisher
        self._settings = settings

    async def run_due(self, now: datetime) -> int:
        """Прогнать все фильтры, у которых наступил интервал. Вернуть число отправок."""
        total = 0
        for filter_id, flt in await self._ps.due_filters(now):
            total += await self._dispatch_one(filter_id, flt, now)
            await self._ps.touch_filter(filter_id, now)
        return total

    async def _dispatch_one(self, filter_id: int, flt: UserFilter, now: datetime) -> int:
        since = now - timedelta(hours=self._settings.personal_lookback_hours)
        listings = await self._storage.recent_listings(flt.city, since)  # newest first
        # Одна квартира с 2-3 площадок → шлём только одну (дешевле; при равной цене
        # приоритет Avito>Cian>Yandex), чтобы не дублировать в личке.
        listings = dedupe_cross_source(listings)
        profile = SearchProfile(name=flt.name, city=flt.city)
        matched = [lst for lst in listings if matches(lst, flt)]  # newest first

        # Две дорожки. СВЕЖИЕ (появились с прошлой проверки) идут вперёд и почти без
        # лимита — их и надо ловить в моменте. Старый БЭКЛОГ досылаем мягко ПОСЛЕ
        # свежих, чтобы он не задерживал моментальные. Первый прогон (last=None) —
        # холодный старт: показываем свежайшие N, остальное само станет бэклогом.
        last = flt.last_checked_at
        cold = last is None
        if cold:
            fresh_cap = self._settings.personal_cold_start_limit
            backlog_cap = 0
        else:
            fresh_cap = self._settings.personal_fresh_max
            # «Только новые»: старый бэклог не досылаем вовсе.
            backlog_cap = (
                self._settings.personal_backlog_per_check if flt.include_backlog else 0
            )

        def _is_fresh(lst: object) -> bool:
            return cold or lst.collected_at > last  # type: ignore[union-attr,operator]

        ordered = sorted(matched, key=lambda lst: not _is_fresh(lst))  # свежие первыми

        sent = sent_fresh = sent_backlog = 0
        handled: set[str] = set()  # content_key, которые уже пометили показанными
        for listing in ordered:
            fresh = _is_fresh(listing)
            # Кап дорожки исчерпан — этот лот пропускаем (свежие капом почти не
            # режутся; бэклог добираем по чуть-чуть).
            if fresh and sent_fresh >= fresh_cap:
                continue
            if not fresh and sent_backlog >= backlog_cap:
                continue
            content_key = listing.content_key
            if await self._ps.was_sent(flt.user_id, content_key):
                continue

            # Триал-гейт: бесплатные отправки исчерпаны и подписка не активна.
            active = await self._ps.is_active(flt.user_id, now)
            if not active:
                used = await self._ps.free_sends_used(flt.user_id)
                if used >= self._settings.free_sends_limit:
                    logger.debug("user %s: триал исчерпан, нужна подписка", flt.user_id)
                    break

            score = await self._scoring.score(listing, profile)
            if not score.is_publishable:  # отсекаем скам/мусор
                await self._ps.mark_sent(flt.user_id, content_key)
                handled.add(content_key)
                continue

            ok = await self._publisher.publish(
                ScoredListing(listing=listing, score=score),
                channel=str(flt.user_id),
                use_custom_emoji=True,  # личка — можно кастом-эмодзи
            )
            await self._ps.mark_sent(flt.user_id, content_key)
            handled.add(content_key)
            if not ok:
                continue
            if not active:
                await self._ps.increment_free_sends(flt.user_id)
            sent += 1
            if fresh:
                sent_fresh += 1
            else:
                sent_backlog += 1

        # Режим «только новые» на холодном старте: остальной бэклог помечаем
        # показанным, чтобы он не капал потом (пользователь выбрал не показывать его).
        if cold and not flt.include_backlog:
            for listing in ordered:
                if listing.content_key not in handled:
                    await self._ps.mark_sent(flt.user_id, listing.content_key)

        if sent:
            logger.info(
                "user %s filter %s: отправлено %d (свежих %d, бэклог %d)",
                flt.user_id,
                filter_id,
                sent,
                sent_fresh,
                sent_backlog,
            )
        return sent
