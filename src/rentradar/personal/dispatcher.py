"""Диспетчер персонального бота.

Раз в N минут (по интервалу каждого фильтра) подбирает новые лоты из уже
собранной БД под критерии пользователя и шлёт в личку. Площадки НЕ дёргаются —
матчинг идёт по `recent_listings`. Для отправки нужен действующий доступ
по времени (пробник, оплата или бонусы). Скам отсекается скорингом.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from weakref import WeakValueDictionary

from ..config import SearchProfile, Settings
from ..interfaces import Publisher, ScoringEngine
from ..models import Listing, ScoredListing, dedupe_cross_source
from ..storage import SqlStorage
from .filters import MIN_INTERVAL_MIN, UserFilter, matches
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
        self._user_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self._dispatch_slots = asyncio.Semaphore(4)

    async def run_due(self, now: datetime) -> int:
        """Прогнать все фильтры, у которых наступил интервал. Вернуть число отправок."""
        users: dict[int, list[int]] = defaultdict(list)
        for filter_id, flt in await self._ps.due_filters(now):
            users[flt.user_id].append(filter_id)
        return await self._dispatch_users(users, now, immediate=False)

    async def run_immediate(self, now: datetime) -> int:
        """Send new matches after a collection pass, ahead of the periodic check."""
        users = {
            flt.user_id: []
            for flt in await self._ps.all_active_filters()
            if flt.clamp_interval() == MIN_INTERVAL_MIN and flt.last_checked_at is not None
        }
        return await self._dispatch_users(users, now, immediate=True)

    async def _dispatch_users(
        self, users: dict[int, list[int]], now: datetime, *, immediate: bool,
    ) -> int:
        async def dispatch_user(user_id: int, due_ids: list[int]) -> int:
            lock = self._user_locks.get(user_id)
            if lock is None:
                lock = asyncio.Lock()
                self._user_locks[user_id] = lock
            async with lock, self._dispatch_slots:
                total = 0
                try:
                    for filter_id, flt in await self._ps.list_filters(user_id):
                        if not flt.active:
                            continue
                        last = flt.last_checked_at
                        if immediate:
                            if last is None or flt.clamp_interval() != MIN_INTERVAL_MIN:
                                continue
                        elif filter_id not in due_ids or (
                            last is not None and now - last < timedelta(minutes=flt.clamp_interval())
                        ):
                            continue
                        sent, complete = await self._dispatch_one(
                            filter_id, flt, now, fresh_only=immediate,
                        )
                        total += sent
                        if complete and not immediate:
                            await self._ps.touch_filter(filter_id, now)
                except Exception:
                    logger.exception("Ошибка рассылки для получателя %s", user_id)
                return total

        return sum(await asyncio.gather(
            *(dispatch_user(user_id, ids) for user_id, ids in users.items())
        ))

    async def _dispatch_one(
        self, filter_id: int, flt: UserFilter, now: datetime, *, fresh_only: bool = False,
    ) -> tuple[int, bool]:
        if not await self._ps.is_active(flt.user_id, now):
            return 0, True
        since = now - timedelta(hours=self._settings.personal_lookback_hours)
        if fresh_only and flt.last_checked_at is not None:
            since = max(since, flt.last_checked_at)
        listings = await self._storage.recent_listings(flt.city, since)  # newest first
        # Одна квартира с 2-3 площадок → шлём только одну (дешевле; при равной цене
        # приоритет Avito>Cian>Yandex), чтобы не дублировать в личке.
        listings = dedupe_cross_source(lst for lst in listings if lst.collected_at <= now)
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

        def _is_fresh(lst: Listing) -> bool:
            return last is None or lst.collected_at > last

        ordered = sorted(matched, key=lambda lst: not _is_fresh(lst))  # свежие первыми

        sent = sent_fresh = sent_backlog = fresh_attempts = backlog_attempts = 0
        access_lost = False
        pending_fresh = False
        handled: set[str] = set()  # content_key, которые уже пометили показанными
        seen = await self._ps.sent_keys(flt.user_id, {lst.content_key for lst in ordered})
        for listing in ordered:
            fresh = _is_fresh(listing)
            if fresh_only and not fresh:
                continue
            content_key = listing.content_key
            if content_key in seen or content_key in handled:
                continue
            # Кап дорожки исчерпан — этот лот пропускаем (свежие капом почти не
            # режутся; бэклог добираем по чуть-чуть).
            if fresh and fresh_attempts >= fresh_cap:
                if not cold:
                    pending_fresh = True
                continue
            if not fresh and backlog_attempts >= backlog_cap:
                break
            if not await self._ps.is_active(flt.user_id, now):
                access_lost = True
                break

            score = await self._scoring.score(listing, profile)
            if not score.is_publishable:  # отсекаем скам/мусор
                await self._ps.mark_sent(flt.user_id, content_key)
                handled.add(content_key)
                continue

            if fresh:
                fresh_attempts += 1
            else:
                backlog_attempts += 1
            ok = await self._publisher.publish(
                ScoredListing(listing=listing, score=score),
                channel=str(flt.user_id),
                use_custom_emoji=True,  # личка — можно кастом-эмодзи
            )
            if not ok and fresh:
                pending_fresh = True
                continue
            await self._ps.mark_sent(flt.user_id, content_key, delivered=ok, now=now)
            handled.add(content_key)
            if not ok:
                continue
            sent += 1
            if fresh:
                sent_fresh += 1
            else:
                sent_backlog += 1

        # Режим «только новые» на холодном старте: остальной бэклог помечаем
        # показанным, чтобы он не капал потом (пользователь выбрал не показывать его).
        if cold and not flt.include_backlog and not access_lost and not pending_fresh:
            for listing in ordered:
                if listing.content_key not in handled and listing.content_key not in seen:
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
        return sent, not pending_fresh
