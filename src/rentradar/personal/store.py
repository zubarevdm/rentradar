"""Хранилище персонального бота поверх того же async-движка SQLAlchemy.

Делит sessionmaker с `SqlStorage` (как `SqlMarketStatsProvider`). Хранит
пользовательские фильтры, статус подписки/триала и пер-юзер журнал отправок.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from sqlalchemy import case, func, select, text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Listing
from ..storage import SqlStorage
from ..storage.orm import ListingRow, ManualPaymentRow, SubscriberRow, UserFilterRow, UserSentRow
from .access import TARIFFS, TRIAL, Access, access_of, add_hours, normalize_pause, utcnow
from .filters import UserFilter, matches


class PersonalStore:
    def __init__(self, storage: SqlStorage, *, pause_max_days: int = 2) -> None:
        self._storage = storage
        self._session = storage._session  # общий sessionmaker
        self._pause_max = pause_max_days  # максимум дней заморозки на паузе

    async def count_new_matches(self, flt: UserFilter, since: datetime) -> int:
        """Сколько свежих лотов подходит под фильтр и ещё не отправлено юзеру."""
        listings = await self._storage.recent_listings(flt.city, since)
        cnt = 0
        for listing in listings:
            if matches(listing, flt) and not await self.was_sent(flt.user_id, listing.content_key):
                cnt += 1
        return cnt

    # ── фильтры ─────────────────────────────────────────────────────────
    async def upsert_filter(
        self, flt: UserFilter, filter_id: int | None = None, *, now: datetime | None = None
    ) -> int:
        saved_id, _ = await self.save_filter(flt, filter_id, now=now)
        return saved_id

    async def save_filter(
        self, flt: UserFilter, filter_id: int | None = None, *, now: datetime | None = None
    ) -> tuple[int, int | None]:
        """Save and activate atomically; return a referrer to notify after commit."""
        async with self._write() as session:
            row = await session.get(UserFilterRow, filter_id) if filter_id is not None else None
            if row is None:
                row = UserFilterRow(user_id=flt.user_id)
                session.add(row)
            _apply_filter(row, flt)
            await session.flush()
            new_id = row.id
            subscriber = await session.get(SubscriberRow, flt.user_id)
            ref_id = (
                await self._activation(session, subscriber, now or utcnow())
                if subscriber is not None
                else None
            )
            return new_id, ref_id

    async def list_filters(self, user_id: int) -> list[tuple[int, UserFilter]]:
        async with self._session() as session:
            rows = (
                await session.execute(select(UserFilterRow).where(UserFilterRow.user_id == user_id))
            ).scalars()
            return [(r.id, _row_to_filter(r)) for r in rows]

    async def get_filter(self, filter_id: int) -> tuple[int, UserFilter] | None:
        async with self._session() as session:
            row = await session.get(UserFilterRow, filter_id)
            return (row.id, _row_to_filter(row)) if row else None

    async def delete_filter(self, filter_id: int, user_id: int) -> bool:
        async with self._session() as session:
            row = await session.get(UserFilterRow, filter_id)
            if row is None or row.user_id != user_id:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def due_filters(self, now: datetime) -> list[tuple[int, UserFilter]]:
        """Активные фильтры, у которых прошёл их интервал с последней проверки."""
        async with self._session() as session:
            rows = (
                await session.execute(select(UserFilterRow).where(UserFilterRow.active.is_(True)))
            ).scalars()
            due: list[tuple[int, UserFilter]] = []
            for r in rows:
                if r.last_checked_at is None:
                    due.append((r.id, _row_to_filter(r)))
                    continue
                elapsed_min = (now - r.last_checked_at).total_seconds() / 60.0
                if elapsed_min >= max(5, r.interval_min):
                    due.append((r.id, _row_to_filter(r)))
            return due

    async def all_active_filters(self) -> list[UserFilter]:
        """Все активные фильтры подписчиков — чтобы решить, какие лоты вообще
        кому-то нужны (для адресного запуска vision)."""
        async with self._session() as session:
            rows = (
                await session.execute(select(UserFilterRow).where(UserFilterRow.active.is_(True)))
            ).scalars()
            return [_row_to_filter(r) for r in rows]

    async def pending_listings(self, listings: list[Listing], now: datetime) -> list[Listing]:
        """Cards with at least one active, matching, unhandled recipient."""
        active = {}
        filters = []
        for flt in await self.all_active_filters():
            if flt.user_id not in active:
                active[flt.user_id] = await self.is_active(flt.user_id, now)
            if active[flt.user_id]:
                filters.append(flt)
        result = []
        for listing in listings:
            for flt in filters:
                if (not flt.include_backlog and flt.last_checked_at is not None
                        and flt.created_at is not None and listing.collected_at < flt.created_at):
                    continue
                if matches(listing, flt) and not await self.was_sent(
                    flt.user_id, listing.content_key,
                ):
                    result.append(listing)
                    break
        return result

    async def admin_stats(self, now: datetime) -> dict[str, int]:
        rows = await self.lifecycle_candidates()
        states = [access_of(r, now, self._pause_max) for r in rows]
        async with self._session() as session:
            filters = (
                await session.scalar(
                    select(func.count())
                    .select_from(UserFilterRow)
                    .where(UserFilterRow.active.is_(True))
                )
                or 0
            )
        return {
            "subscribers": len(rows),
            "paid": sum(
                a.active(now) and not (a.trial_until and a.trial_until > now) for a in states
            ),
            "trial": sum(bool(a.trial_until and a.trial_until > now) for a in states),
            "paused": sum(a.paused for a in states),
            "expired": sum(not a.active(now) for a in states),
            "active_filters": filters,
        }

    async def touch_filter(self, filter_id: int, now: datetime) -> None:
        async with self._session() as session:
            row = await session.get(UserFilterRow, filter_id)
            if row is not None:
                row.last_checked_at = now
                await session.commit()

    # ── подписка / триал ────────────────────────────────────────────────
    @asynccontextmanager
    async def _write(self) -> AsyncIterator[AsyncSession]:
        # SQLite serializes the entire read/modify/write operation across processes.
        async with self._session() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def get_or_create_subscriber(
        self, telegram_id: int, username: str | None = None, *, referred_by: int | None = None
    ) -> SubscriberRow:
        async with self._write() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row is None:
                ref = (
                    await session.get(SubscriberRow, referred_by)
                    if referred_by and 0 < referred_by < 2**63 and referred_by != telegram_id
                    else None
                )
                row = SubscriberRow(
                    telegram_id=telegram_id,
                    username=username,
                    referred_by=ref.telegram_id if ref else None,
                )
                session.add(row)
                await session.flush()
            return row

    async def access(self, telegram_id: int, now: datetime) -> Access:
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            return access_of(row, now, self._pause_max) if row else Access(None, None, False)

    async def is_active(self, telegram_id: int, now: datetime) -> bool:
        return (await self.access(telegram_id, now)).active(now)

    async def is_paused(self, telegram_id: int, now: datetime) -> bool:
        return (await self.access(telegram_id, now)).paused

    async def paid_until_of(self, telegram_id: int) -> datetime | None:
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            return row.paid_until if row else None

    async def frozen_days_left(self, telegram_id: int) -> int:
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row and row.paused_at and row.paid_until:
                return max(0, (row.paid_until - row.paused_at).days)
            return 0

    async def pause(self, telegram_id: int, now: datetime) -> bool:
        async with self._write() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row is None:
                return False
            normalize_pause(row, now, self._pause_max)
            if row.paused_at is not None or row.paid_until is None or row.paid_until <= now:
                return False
            # Deferred paid time starts after trial; never freeze the trial itself.
            remaining = row.paid_until - max(now, row.access_starts_at or now)
            if remaining <= timedelta(0):
                return False
            row.paid_until = now + remaining
            row.access_starts_at = None
            row.paused_at = now
            return True

    async def resume(self, telegram_id: int, now: datetime) -> bool:
        async with self._write() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row is None or row.paused_at is None:
                return False
            frozen = min(now - row.paused_at, timedelta(days=self._pause_max))
            resumed_at = row.paused_at + frozen
            remaining = max(timedelta(0), (row.paid_until or row.paused_at) - row.paused_at)
            row.access_starts_at = max(resumed_at, row.trial_until or resumed_at)
            row.paid_until = row.access_starts_at + remaining
            row.paused_at = None
            return True

    async def free_sends_used(self, telegram_id: int) -> int:
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            return row.free_sends_used if row else 0

    async def increment_free_sends(self, telegram_id: int, n: int = 1) -> None:
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row is not None:
                row.free_sends_used += n
                await session.commit()

    async def set_paid_until(self, telegram_id: int, until: datetime) -> None:
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row is None:
                row = SubscriberRow(telegram_id=telegram_id)
                session.add(row)
            row.paid_until = until
            # Новый платёжный цикл → сбрасываем напоминания об окончании/возврате.
            row.nudged_expiry = False
            row.nudged_winback = False
            await session.commit()

    # ── lifecycle-напоминания ───────────────────────────────────────────
    async def lifecycle_candidates(self) -> list[SubscriberRow]:
        """Все подписчики — джоб сам решает, кому и что напомнить."""
        async with self._session() as session:
            rows = (await session.execute(select(SubscriberRow))).scalars()
            return list(rows)

    async def mark_nudged(self, telegram_id: int, kind: str) -> None:
        """Отметить, что напоминание вида kind (trial/expiry/winback) отправлено."""
        field = {
            "trial_soon": "nudged_trial_soon",
            "trial": "nudged_trial",
            "expiry": "nudged_expiry",
            "winback": "nudged_winback",
        }
        attr = field.get(kind)
        if attr is None:
            return
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row is not None:
                setattr(row, attr, True)
                await session.commit()

    async def verify_channel(self, telegram_id: int, now: datetime) -> bool:
        """Call only after successful membership verification (or explicit dev mode)."""
        granted, _ = await self.verify_channel_with_reward(telegram_id, now)
        return granted

    async def verify_channel_with_reward(
        self, telegram_id: int, now: datetime
    ) -> tuple[bool, int | None]:
        async with self._write() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row is None:
                row = SubscriberRow(telegram_id=telegram_id)
                session.add(row)
                await session.flush()
            row.channel_verified_at = row.channel_verified_at or now
            granted = bool(row.trial_eligible and row.trial_until is None)
            if granted:
                normalize_pause(row, now, self._pause_max)
                row.trial_until = now + TRIAL
                row.trial_eligible = False
                row.channel_bonus_claimed = True
                row.nudged_trial = False
                row.nudged_trial_soon = False
                if row.paused_at is None and row.paid_until and row.paid_until > now:
                    remaining = row.paid_until - max(now, row.access_starts_at or now)
                    row.access_starts_at = row.trial_until
                    row.paid_until = row.trial_until + remaining
            ref_id = await self._activation(session, row, now)
            return granted, ref_id

    async def grant_hours(self, telegram_id: int, now: datetime, hours: float) -> datetime:
        if hours <= 0:
            raise ValueError("Срок должен быть положительным")
        async with self._write() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row is None:
                raise ValueError("Пользователь не найден: сначала нужен /start")
            add_hours(row, now, hours, self._pause_max)
            assert row.paid_until is not None
            return row.paid_until

    async def confirm_payment(
        self,
        telegram_id: int,
        tariff: str,
        operation_id: str,
        admin_id: int,
        admins: Collection[int],
        now: datetime,
    ) -> ManualPaymentRow:
        if admin_id not in admins:
            raise PermissionError("Только для администратора")
        if tariff not in TARIFFS:
            raise ValueError("Неизвестный тариф")
        if telegram_id <= 0 or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", operation_id):
            raise ValueError("Некорректный ID пользователя или операции")
        plan = TARIFFS[tariff]
        async with self._write() as session:
            previous = await session.get(ManualPaymentRow, operation_id)
            if previous:
                if previous.user_id != telegram_id or previous.tariff != tariff:
                    raise ValueError("Этот operation_id уже использован для другой оплаты")
                return previous
            row = await session.get(SubscriberRow, telegram_id)
            if row is None:
                raise ValueError("Пользователь не найден: сначала нужен /start")
            add_hours(row, now, plan.hours, self._pause_max)
            ref_id = None
            if row.referred_by is not None and not row.referral_credited:
                ref = await session.get(SubscriberRow, row.referred_by)
                if ref is not None:
                    add_hours(ref, now, 48, self._pause_max)
                    row.referral_credited = True
                    ref_id = ref.telegram_id
            assert row.paid_until is not None
            payment = ManualPaymentRow(
                operation_id=operation_id,
                user_id=telegram_id,
                tariff=tariff,
                admin_id=admin_id,
                price_rub=plan.price,
                hours=plan.hours,
                confirmed_at=now,
                result_until=row.paid_until,
                referrer_id=ref_id,
            )
            session.add(payment)
            await session.flush()
            return payment

    async def _activation(
        self, session: AsyncSession, row: SubscriberRow, now: datetime
    ) -> int | None:
        if row.referred_by is None or row.referral_activated or not row.channel_verified_at:
            return None
        has_filter = await session.scalar(
            select(UserFilterRow.id).where(UserFilterRow.user_id == row.telegram_id).limit(1)
        )
        if has_filter is None:
            return None
        ref = await session.get(SubscriberRow, row.referred_by)
        if ref is None:
            return None
        add_hours(ref, now, 12, self._pause_max)
        row.referral_activated = True
        return ref.telegram_id

    # ── справочник станций из реальных данных ───────────────────────────
    async def distinct_metros(self, city: str) -> list[str]:
        """Уникальные станции метро города из собранных лотов (для подсказок)."""
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(ListingRow.metro)
                    .where(ListingRow.city == city)
                    .where(ListingRow.metro.is_not(None))
                    .distinct()
                )
            ).scalars()
            return sorted({m for m in rows if m})

    # ── пер-юзер дедуп отправок ─────────────────────────────────────────
    async def was_sent(self, user_id: int, content_key: str) -> bool:
        async with self._session() as session:
            found = await session.scalar(
                select(UserSentRow.id)
                .where(UserSentRow.user_id == user_id)
                .where(UserSentRow.content_key == content_key)
            )
            return found is not None

    async def mark_sent(
        self,
        user_id: int,
        content_key: str,
        *,
        delivered: bool = False,
        now: datetime | None = None,
    ) -> None:
        # Repeated writes must be safe, including concurrent cycles and backlog.
        values = dict(user_id=user_id, content_key=content_key, delivered=delivered,
                      sent_at=now or utcnow())
        statement = insert(UserSentRow).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[UserSentRow.user_id, UserSentRow.content_key],
            set_={
                "delivered": UserSentRow.delivered | statement.excluded.delivered,
                "sent_at": case(
                    ((~UserSentRow.delivered) & statement.excluded.delivered,
                     statement.excluded.sent_at),
                    else_=UserSentRow.sent_at,
                ),
            },
        )
        async with self._session() as session:
            await session.execute(statement)
            await session.commit()

    async def count_delivered(self, user_id: int | None, since: datetime) -> int:
        """Сколько объявлений реально доставлено с момента since (None — всем)."""
        query = (
            select(func.count())
            .select_from(UserSentRow)
            .where(UserSentRow.delivered.is_(True))
            .where(UserSentRow.sent_at >= since)
        )
        if user_id is not None:
            query = query.where(UserSentRow.user_id == user_id)
        async with self._session() as session:
            return await session.scalar(query) or 0


# ── конвертеры ───────────────────────────────────────────────────────────
def _apply_filter(row: UserFilterRow, flt: UserFilter) -> None:
    row.user_id = flt.user_id
    row.name = flt.name
    row.city = flt.city
    row.rooms = list(flt.rooms)
    row.price_min = flt.price_min
    row.price_max = flt.price_max
    row.area_min = flt.area_min
    row.area_max = flt.area_max
    row.metros = list(flt.metros)
    row.districts = list(flt.districts)
    row.max_metro_min = flt.max_metro_min
    row.renovation_min = None  # MVP: legacy column, no active filter
    row.no_commission = flt.no_commission
    row.interval_min = flt.clamp_interval()
    row.active = flt.active
    row.include_backlog = flt.include_backlog


def _row_to_filter(row: UserFilterRow) -> UserFilter:
    return UserFilter(
        user_id=row.user_id,
        name=row.name,
        city=row.city,
        rooms=list(row.rooms or []),
        price_min=row.price_min,
        price_max=row.price_max,
        area_min=row.area_min,
        area_max=row.area_max,
        metros=list(row.metros or []),
        districts=list(row.districts or []),
        max_metro_min=row.max_metro_min,
        # NULL у старых строк (колонка добавлена миграцией позже) → False, иначе
        # pydantic валится и роняет ВСЁ, что грузит фильтры (vision, рассылку).
        no_commission=bool(row.no_commission),
        interval_min=row.interval_min,
        active=bool(row.active),
        # NULL у старых строк → True (по умолчанию показываем всё).
        include_backlog=row.include_backlog if row.include_backlog is not None else True,
        last_checked_at=row.last_checked_at,
        created_at=row.created_at,
    )
