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
from datetime import UTC, datetime, timedelta
from time import monotonic
from weakref import WeakValueDictionary

from ..config import SearchProfile, Settings
from ..errors import DeliveryDeferred, DeliveryUncertain
from ..interfaces import Publisher, ScoringEngine
from ..models import Listing, ScoredListing, source_rank
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
        self._pending: dict[int, dict[tuple[str, str], Listing]] = {}
        self._workers: dict[int, asyncio.Task] = {}

    async def queue_changes(self, listings: list[Listing]) -> None:
        """A slow chat holds only its own batch. The scheduled scan recovers after restart."""
        if getattr(self._publisher, "simulated", False):
            return
        users = {flt.user_id for flt in await self._ps.all_active_filters()
                 if flt.clamp_interval() == MIN_INTERVAL_MIN and flt.last_checked_at is not None}
        for user_id in users:
            batch = self._pending.setdefault(user_id, {})
            batch.update(((lst.source.value, lst.external_id), lst) for lst in listings)
            if user_id not in self._workers:
                self._workers[user_id] = asyncio.create_task(self._drain_changes(user_id))

    async def _drain_changes(self, user_id: int) -> None:
        try:
            while batch := self._pending.pop(user_id, None):
                await self._dispatch_users(
                    {user_id: []}, datetime.now(UTC).replace(tzinfo=None),
                    immediate=True, batch=list(batch.values()),
                )
        finally:
            self._workers.pop(user_id, None)

    async def close(self) -> None:
        tasks = list(self._workers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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
        batch: list[Listing] | None = None,
    ) -> int:
        started = monotonic()

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
                            last is not None
                            and now - last < timedelta(minutes=flt.clamp_interval())
                        ):
                            continue
                        sent, complete = await self._dispatch_one(
                            filter_id, flt, now, started=started, fresh_only=immediate, batch=batch,
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
        self,
        filter_id: int,
        flt: UserFilter,
        now: datetime,
        *,
        started: float,
        fresh_only: bool = False,
        batch: list[Listing] | None = None,
    ) -> tuple[int, bool]:
        if getattr(self._publisher, "simulated", False):
            return 0, False
        if not await self._ps.is_active(flt.user_id, now):
            return 0, True
        since = now - timedelta(hours=self._settings.personal_lookback_hours)
        attempts = await self._ps.attempts_for(flt.user_id)
        if (fresh_only or not flt.include_backlog) and flt.last_checked_at and not attempts:
            since = max(since, flt.last_checked_at)
        listings = (batch if batch is not None else
                    await self._ps.candidates(flt, since))
        profile = SearchProfile(name=flt.name, city=flt.city)
        copies: dict[str, list[Listing]] = defaultdict(list)
        for listing in listings:
            if listing.collected_at <= now and listing.photos and matches(listing, flt):
                copies[listing.content_key].append(listing)
        matched = [
            listing
            for group in copies.values()
            for listing in sorted(
                group, key=lambda item: source_rank(item.source.value, item.price)
            )
        ]

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
            return (
                batch is not None
                or last is None
                or lst.collected_at > last
                or lst.content_key in attempts
            )

        ordered = sorted(matched, key=lambda lst: not _is_fresh(lst))  # свежие первыми

        sent = sent_fresh = sent_backlog = fresh_attempts = backlog_attempts = 0
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
            attempt = attempts.get(content_key)
            if attempt and attempt.retry_at > now:
                pending_fresh = True
                continue
            if not await self._ps.is_active(
                flt.user_id, now + timedelta(seconds=monotonic() - started)
            ):
                return sent, False

            current = await self._storage.get_listing(listing.source.value, listing.external_id)
            if current is None or not matches(current, flt):
                continue
            listing = current
            score = await self._scoring.score(listing, profile)
            if not score.is_publishable:  # отсекаем скам/мусор
                # A later photo, price or description can make this listing eligible.
                continue

            if fresh:
                fresh_attempts += 1
            else:
                backlog_attempts += 1
            sending_at = now + timedelta(seconds=monotonic() - started)
            if not await self._ps.claim_delivery(flt.user_id, content_key, sending_at):
                continue
            try:
                ok = await self._publisher.publish(
                    ScoredListing(listing=listing, score=score),
                    channel=str(flt.user_id), use_custom_emoji=True,
                )
            except DeliveryDeferred as exc:
                ok = False
                state, delay = "rate_limited", timedelta(seconds=exc.seconds)
            except DeliveryUncertain:
                ok = False
                state, delay = "uncertain", timedelta(minutes=15)
            else:
                state = "retry"
                delay = timedelta(
                    seconds=min(900, 20 * 2 ** min(attempt.attempts if attempt else 0, 6))
                )
            finished = now + timedelta(seconds=monotonic() - started)
            if not ok:
                await self._ps.defer_delivery(flt.user_id, content_key, state, finished + delay)
                pending_fresh = True
                continue
            await self._ps.mark_sent(flt.user_id, content_key, delivered=True, now=finished)
            logger.info(
                "delivery_ack user=%s source=%s collected_to_ack_seconds=%.2f",
                flt.user_id,
                listing.source.value,
                (finished - (listing.first_seen_at or listing.collected_at)).total_seconds(),
            )
            handled.add(content_key)
            sent += 1
            if fresh:
                sent_fresh += 1
            else:
                sent_backlog += 1

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
