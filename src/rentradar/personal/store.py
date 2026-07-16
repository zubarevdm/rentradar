"""Хранилище персонального бота поверх того же async-движка SQLAlchemy.

Делит sessionmaker с `SqlStorage` (как `SqlMarketStatsProvider`). Хранит
пользовательские фильтры, статус подписки/триала и пер-юзер журнал отправок.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from ..storage import SqlStorage
from ..storage.orm import ListingRow, SubscriberRow, UserFilterRow, UserSentRow
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
            if matches(listing, flt) and not await self.was_sent(
                flt.user_id, listing.content_key
            ):
                cnt += 1
        return cnt

    # ── фильтры ─────────────────────────────────────────────────────────
    async def upsert_filter(self, flt: UserFilter, filter_id: int | None = None) -> int:
        async with self._session() as session:
            row = (
                await session.get(UserFilterRow, filter_id) if filter_id is not None else None
            )
            if row is None:
                row = UserFilterRow(user_id=flt.user_id)
                session.add(row)
            _apply_filter(row, flt)
            await session.flush()
            new_id = row.id
            await session.commit()
            return new_id

    async def list_filters(self, user_id: int) -> list[tuple[int, UserFilter]]:
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(UserFilterRow).where(UserFilterRow.user_id == user_id)
                )
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
                await session.execute(
                    select(UserFilterRow).where(UserFilterRow.active.is_(True))
                )
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
                await session.execute(
                    select(UserFilterRow).where(UserFilterRow.active.is_(True))
                )
            ).scalars()
            return [_row_to_filter(r) for r in rows]

    async def admin_stats(self, now: datetime) -> dict[str, int]:
        """Сводка для админской команды /stats: подписчики и фильтры."""
        async with self._session() as session:
            total = await session.scalar(
                select(func.count()).select_from(SubscriberRow)
            ) or 0
            paid = await session.scalar(
                select(func.count())
                .select_from(SubscriberRow)
                .where(SubscriberRow.paid_until.is_not(None))
                .where(SubscriberRow.paid_until > now)
            ) or 0
            paused = await session.scalar(
                select(func.count())
                .select_from(SubscriberRow)
                .where(SubscriberRow.paused_at.is_not(None))
            ) or 0
            trial = await session.scalar(
                select(func.count())
                .select_from(SubscriberRow)
                .where(SubscriberRow.paid_until.is_(None))
            ) or 0
            expired = await session.scalar(
                select(func.count())
                .select_from(SubscriberRow)
                .where(SubscriberRow.paid_until.is_not(None))
                .where(SubscriberRow.paid_until <= now)
            ) or 0
            active_filters = await session.scalar(
                select(func.count())
                .select_from(UserFilterRow)
                .where(UserFilterRow.active.is_(True))
            ) or 0
        # paid + trial + expired = subscribers; paused — оверлей (подмножество paid).
        return {
            "subscribers": total,
            "paid": paid,
            "trial": trial,
            "expired": expired,
            "paused": paused,
            "active_filters": active_filters,
        }

    async def touch_filter(self, filter_id: int, now: datetime) -> None:
        async with self._session() as session:
            row = await session.get(UserFilterRow, filter_id)
            if row is not None:
                row.last_checked_at = now
                await session.commit()

    # ── подписка / триал ────────────────────────────────────────────────
    async def get_or_create_subscriber(
        self, telegram_id: int, username: str | None = None
    ) -> SubscriberRow:
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row is None:
                row = SubscriberRow(telegram_id=telegram_id, username=username)
                session.add(row)
                await session.commit()
                row = await session.get(SubscriberRow, telegram_id)
            assert row is not None
            return row

    async def is_active(self, telegram_id: int, now: datetime) -> bool:
        """Подписка действует: оплачена, не истекла и НЕ на паузе. Если пауза длится
        дольше лимита — авто-возобновляем (добавляем замороженные дни)."""
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row is None:
                return False
            if row.paused_at is not None:
                cap = timedelta(days=self._pause_max)
                if now - row.paused_at >= cap:  # пауза выдохлась → возобновляем
                    row.paid_until = (row.paid_until or now) + cap
                    row.paused_at = None
                    await session.commit()
            active = row.paused_at is None and row.paid_until is not None
            return bool(active and row.paid_until > now)

    async def is_paused(self, telegram_id: int, now: datetime) -> bool:
        """На паузе и лимит паузы ещё не вышел."""
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            return bool(
                row
                and row.paused_at is not None
                and now - row.paused_at < timedelta(days=self._pause_max)
            )

    async def paid_until_of(self, telegram_id: int) -> datetime | None:
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            return row.paid_until if row else None

    async def frozen_days_left(self, telegram_id: int) -> int:
        """Сколько оплаченных дней заморожено на паузе (для показа в статусе)."""
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row and row.paused_at and row.paid_until:
                return max(0, (row.paid_until - row.paused_at).days)
            return 0

    async def pause(self, telegram_id: int, now: datetime) -> bool:
        """Поставить активную подписку на паузу (заморозить дни). True — если поставили."""
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row and row.paused_at is None and row.paid_until and row.paid_until > now:
                row.paused_at = now
                await session.commit()
                return True
            return False

    async def resume(self, telegram_id: int, now: datetime) -> bool:
        """Снять с паузы: вернуть замороженные дни (не более лимита). True — если сняли."""
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row and row.paused_at is not None:
                frozen = min(now - row.paused_at, timedelta(days=self._pause_max))
                row.paid_until = (row.paid_until or now) + frozen
                row.paused_at = None
                await session.commit()
                return True
            return False

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
        field = {"trial": "nudged_trial", "expiry": "nudged_expiry", "winback": "nudged_winback"}
        attr = field.get(kind)
        if attr is None:
            return
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row is not None:
                setattr(row, attr, True)
                await session.commit()

    async def claim_channel_bonus(self, telegram_id: int, now: datetime, days: int) -> bool:
        """Разово начислить бонус за подписку на канал. False, если уже получал."""
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row is None:
                row = SubscriberRow(telegram_id=telegram_id)
                session.add(row)
            if row.channel_bonus_claimed:
                return False
            base = row.paid_until if (row.paid_until and row.paid_until > now) else now
            row.paid_until = base + timedelta(days=days)
            row.channel_bonus_claimed = True
            await session.commit()
            return True

    # ── реферальная программа ───────────────────────────────────────────
    async def set_referred_by(self, telegram_id: int, referrer_id: int) -> None:
        """Запомнить, кто пригласил (только при первом /start и не сам себя)."""
        if referrer_id == telegram_id:
            return
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row is not None and row.referred_by is None and row.free_sends_used == 0:
                row.referred_by = referrer_id
                await session.commit()

    async def credit_referral(self, telegram_id: int, now: datetime, days: int) -> int | None:
        """Начислить рефереру +days при первой оплате приглашённого. Возвращает id
        реферера (чтобы уведомить) или None, если начислять некому/уже начислено."""
        async with self._session() as session:
            row = await session.get(SubscriberRow, telegram_id)
            if row is None or row.referred_by is None or row.referral_credited:
                return None
            ref = await session.get(SubscriberRow, row.referred_by)
            if ref is None:
                return None
            base = ref.paid_until if (ref.paid_until and ref.paid_until > now) else now
            ref.paid_until = base + timedelta(days=days)
            row.referral_credited = True
            await session.commit()
            return row.referred_by

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

    async def mark_sent(self, user_id: int, content_key: str) -> None:
        async with self._session() as session:
            session.add(UserSentRow(user_id=user_id, content_key=content_key))
            await session.commit()


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
    row.renovation_min = flt.renovation_min
    row.no_commission = flt.no_commission
    row.interval_min = flt.clamp_interval()
    row.active = flt.active


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
        renovation_min=row.renovation_min,
        # NULL у старых строк (колонка добавлена миграцией позже) → False, иначе
        # pydantic валится и роняет ВСЁ, что грузит фильтры (vision, рассылку).
        no_commission=bool(row.no_commission),
        interval_min=row.interval_min,
        active=bool(row.active),
        last_checked_at=row.last_checked_at,
    )
