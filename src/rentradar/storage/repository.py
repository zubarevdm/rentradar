"""Реализация `Storage` поверх async SQLAlchemy/SQLite.

Дедуп выполняется по `Listing.fingerprint`: `upsert_listings` возвращает только
те объявления, чей отпечаток ещё не встречался, — именно они идут дальше по
конвейеру (скоринг/публикация).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from ..interfaces import MarketStatsProvider, Storage
from ..models import (
    DealType,
    Listing,
    PropertyType,
    Source,
    build_segment_keys,
)
from .orm import Base, ListingRow, MarketStatRow, PostedRow


class SqlStorage(Storage):
    """SQLite-хранилище (async). На рост — заменить URL на Postgres, код тот же."""

    def __init__(self, db_url: str, db_path: Path | None = None) -> None:
        if db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine: AsyncEngine = create_async_engine(db_url, future=True)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def init(self, *, migration_now: datetime | None = None, pause_max_days: int = 2) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_migrate_add_missing_columns)
            await conn.run_sync(_migrate_rental_terms)
            await conn.run_sync(_migrate_walking_metro)
            await conn.run_sync(
                lambda connection: _migrate_access(connection, migration_now, pause_max_days)
            )
            await conn.run_sync(_backfill_flags)

    async def upsert_listings(self, listings: Sequence[Listing]) -> list[Listing]:
        """Сохранить НОВЫЕ лоты; вернуть их. Дубль определяется по (source,
        external_id) — это и уникальный ключ в БД. По fingerprint нельзя: при
        смене цены fingerprint меняется, и тот же лот ловил UNIQUE-конфликт."""
        new_items: list[Listing] = []
        seen_keys: set[tuple[str, str]] = set()
        async with self._session() as session:
            for listing in listings:
                key = (listing.source.value, listing.external_id)
                if key in seen_keys:  # дубль внутри батча
                    continue
                row = await session.scalar(
                    select(ListingRow).where(
                        ListingRow.source == listing.source.value,
                        ListingRow.external_id == listing.external_id,
                    )
                )
                if row is not None:
                    if row.price != listing.price:
                        row.price = listing.price
                        row.fingerprint = listing.fingerprint
                        row.collected_at = listing.collected_at
                        if listing.source == Source.AVITO:
                            # An old deposit expressed in monthly rents was
                            # computed against the previous price. Re-read the
                            # same detail through the existing bounded queue.
                            row.deposit_rub = None
                            row.commission_pct = None
                            row.commission_rub = None
                            row.deposit_due_rub = None
                            row.deposit_installments = False
                            row.upfront_payments = {}
                            row.enriched_at = None
                        else:
                            # These collectors provide payment fields on each
                            # search card, so use their current snapshot.
                            row.deposit_rub = listing.deposit_rub
                            row.commission_pct = listing.commission_pct
                            row.commission_rub = listing.commission_rub
                    # Collectors now guarantee walking minutes for this station.
                    # Replace even with None: a newly unknown/transport-only time
                    # must not retain a stale walking value from a previous card.
                    row.metro = listing.metro
                    row.metro_distance_min = listing.metro_distance_min
                    row.metro_line = listing.metro_line
                    row.metro_line_color = listing.metro_line_color
                    if listing.photos:
                        row.photos = listing.photos
                    if listing.source == Source.AVITO and row.enriched_at is None:
                        # A repeated search card can add terms even when its price
                        # did not change. Keep a richer detail snapshot if present.
                        for field in (
                            "deposit_rub", "commission_pct", "commission_rub",
                            "deposit_due_rub", "deposit_installments", "upfront_payments",
                            "utilities_rub", "utilities_included", "meters_included",
                            "description",
                        ):
                            if field in listing.model_fields_set:
                                setattr(row, field, getattr(listing, field))
                    elif listing.source == Source.AVITO:
                        # Older partial details may have been marked enriched.
                        # Fresh explicit search fields can fill their missing terms.
                        if row.deposit_rub is None and listing.deposit_rub is not None:
                            row.deposit_rub = listing.deposit_rub
                        if (row.commission_pct is None and row.commission_rub is None
                                and listing.commission_amount is not None):
                            row.commission_pct = listing.commission_pct
                            row.commission_rub = listing.commission_rub
                    continue
                seen_keys.add(key)
                session.add(_to_row(listing))
                new_items.append(listing)
            await session.commit()
        return new_items

    async def get_listing(self, source: str, external_id: str) -> Listing | None:
        """Read the latest cached conditions before sending a queued candidate."""
        async with self._session() as session:
            row = await session.scalar(select(ListingRow).where(
                ListingRow.source == source, ListingRow.external_id == external_id,
            ))
            return _row_to_listing(row) if row is not None else None

    async def was_posted(self, fingerprint: str) -> bool:
        async with self._session() as session:
            found = await session.scalar(
                select(PostedRow.id).where(PostedRow.fingerprint == fingerprint)
            )
            return found is not None

    async def mark_posted(self, fingerprint: str, channel: str) -> None:
        async with self._session() as session:
            session.add(PostedRow(fingerprint=fingerprint, channel=channel))
            await session.commit()

    async def count_listings(self) -> int:
        async with self._session() as session:
            return await session.scalar(select(func.count()).select_from(ListingRow)) or 0

    async def recent_listings(self, city: str, since: datetime) -> list[Listing]:
        """Лоты города, собранные не раньше `since`, в порядке свежести публикации.

        Сортируем по `published_at` (реальная дата публикации на площадке), а не по
        `collected_at` — иначе площадка, которую мы дособираем чаще, систематически
        вытесняет остальные. `published_at DESC` в SQLite ставит NULL в конец.
        """
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(ListingRow)
                    .where(ListingRow.city == city)
                    .where(ListingRow.collected_at >= since)
                    .order_by(ListingRow.published_at.desc(), ListingRow.collected_at.desc())
                )
            ).scalars()
            return [_row_to_listing(r) for r in rows]

    async def unanalyzed_listings(self, city: str, since: datetime, limit: int) -> list[Listing]:
        """Свежие (по `since`) лоты города, ещё не прошедшие vision-анализ.
        Старейшие сначала — чтобы очередь анализа двигалась честно (FIFO)."""
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(ListingRow)
                    .where(ListingRow.city == city)
                    .where(ListingRow.collected_at >= since)
                    .where(ListingRow.analyzed_at.is_(None))
                    .order_by(ListingRow.collected_at.asc())
                    .limit(limit)
                )
            ).scalars()
            return [_row_to_listing(r) for r in rows]

    async def unenriched_listings(self, source: str, since: datetime, limit: int) -> list[Listing]:
        """Свежие лоты площадки, ещё не дообогащённые с детальной страницы.
        Новейшие сначала — свежак важнее для постинга, хвост догоним позже."""
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(ListingRow)
                    .where(ListingRow.source == source)
                    .where(ListingRow.collected_at >= since)
                    .where(ListingRow.enriched_at.is_(None))
                    .order_by(ListingRow.collected_at.desc())
                    .limit(limit)
                )
            ).scalars()
            return [_row_to_listing(r) for r in rows]

    async def save_enrichment(
        self,
        source: str,
        external_id: str,
        *,
        enriched_at: datetime,
        description: str | None = None,
        deposit_rub: int | None = None,
        commission_pct: int | None = None,
        commission_rub: int | None = None,
        deposit_due_rub: int | None = None,
        deposit_installments: bool = False,
        upfront_payments: dict[str, int | None] | None = None,
        utilities_rub: int | None = None,
        utilities_included: bool | None = None,
        meters_included: bool | None = None,
        photos: list[str] | None = None,
        deposit_present: bool = True,
        commission_present: bool = True,
    ) -> None:
        """Save a successfully fetched detail's payment snapshot, including unknowns.

        An explicit unknown payment clears an earlier amount. An absent field in
        partial detail markup retains the search-card value, including zero.
        Network failures never call this method.
        Missing description/photos and utility information retain their old values.
        """
        async with self._session() as session:
            row = await session.scalar(
                select(ListingRow)
                .where(ListingRow.source == source)
                .where(ListingRow.external_id == external_id)
            )
            if row is None:
                return
            if description is not None:
                row.description = description[:4000]
            if deposit_present:
                row.deposit_rub = deposit_rub
                row.deposit_due_rub = deposit_due_rub
                row.deposit_installments = deposit_installments
            # The two representations describe the same payment. A fresh value
            # must supersede its old representation (including an explicit zero).
            if commission_present and commission_rub is not None:
                row.commission_rub = commission_rub
                row.commission_pct = None
            elif commission_present:
                row.commission_pct = commission_pct
                row.commission_rub = None
            for field, value in {
                "utilities_rub": utilities_rub, "utilities_included": utilities_included,
            }.items():
                if value is not None:
                    setattr(row, field, value)
            row.upfront_payments = upfront_payments or {}
            if meters_included is not None:
                row.meters_included = meters_included
            if photos:
                row.photos = photos
            incomplete = (
                (not deposit_present and row.deposit_rub is None)
                or (not commission_present and row.commission_pct is None
                    and row.commission_rub is None)
            )
            row.enriched_at = None if incomplete else enriched_at
            await session.commit()

    async def save_photo_analysis(
        self,
        source: str,
        external_id: str,
        *,
        renovation: str | None,
        has_furniture: bool | None,
        appeal: int | None,
        best_photos: list[str],
        analyzed_at: datetime,
        flagged: bool = False,
    ) -> None:
        """Записать результат vision-анализа в лот (и пометить как проанализированный)."""
        async with self._session() as session:
            row = await session.scalar(
                select(ListingRow)
                .where(ListingRow.source == source)
                .where(ListingRow.external_id == external_id)
            )
            if row is None:
                return
            row.renovation = renovation
            row.has_furniture = has_furniture
            row.appeal = appeal
            row.best_photos = best_photos
            row.flagged = flagged
            row.analyzed_at = analyzed_at
            await session.commit()

    async def recompute_market_stats(self, *, window_days: int | None = None) -> int:
        """Пересчитать медианы по сегментам из накопленных объявлений.

        Группируем `listings` по `segment_key`, считаем медиану цены и медиану
        ₽/м², пишем в `market_stats`. Возвращаем число обновлённых сегментов.

        `window_days` — учитывать только лоты, собранные за последние N дней (чтобы
        старые цены не тянули медиану). None = по всей истории (данные при этом НЕ
        удаляются — окно влияет только на расчёт медиан, не на хранение).
        """
        async with self._session() as session:
            query = select(
                ListingRow.city,
                ListingRow.district,
                ListingRow.rooms,
                ListingRow.price,
                ListingRow.area,
            )
            if window_days is not None:
                since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=window_days)
                query = query.where(ListingRow.collected_at >= since)
            rows = (await session.execute(query)).all()

            prices: dict[str, list[int]] = {}
            ppm: dict[str, list[float]] = {}
            # Каждое объявление вносит вклад во ВСЕ уровни иерархии (точный,
            # район+комнаты, город+комнаты) — так грубые сегменты набирают
            # статзначимость и работают как fallback.
            for city, district, rooms, price, area in rows:
                for key in build_segment_keys(city, district, rooms, area):
                    prices.setdefault(key, []).append(price)
                    if area and area > 0:
                        ppm.setdefault(key, []).append(price / area)

            for segment_key, price_list in prices.items():
                ppm_list = ppm.get(segment_key, [])
                stat = await session.get(MarketStatRow, segment_key)
                if stat is None:
                    stat = MarketStatRow(segment_key=segment_key)
                    session.add(stat)
                stat.median_price = float(median(price_list))
                stat.median_price_per_m2 = float(median(ppm_list)) if ppm_list else None
                stat.sample_size = len(price_list)

            await session.commit()
            return len(prices)

    async def dispose(self) -> None:
        await self._engine.dispose()


class SqlMarketStatsProvider(MarketStatsProvider):
    """Читает предрассчитанные медианы из таблицы `market_stats`."""

    def __init__(self, storage: SqlStorage) -> None:
        self._session = storage._session  # общий sessionmaker

    async def _get(self, segment_key: str) -> MarketStatRow | None:
        async with self._session() as session:
            return await session.get(MarketStatRow, segment_key)

    async def median_price(self, segment_key: str) -> float | None:
        row = await self._get(segment_key)
        return row.median_price if row else None

    async def median_price_per_m2(self, segment_key: str) -> float | None:
        row = await self._get(segment_key)
        return row.median_price_per_m2 if row else None

    async def sample_size(self, segment_key: str) -> int:
        row = await self._get(segment_key)
        return row.sample_size if row else 0


def _migrate_access(connection: Connection, now: datetime | None, pause_max_days: int) -> None:
    from sqlalchemy import text

    stamp = now or datetime.now(UTC).replace(tzinfo=None)
    version = "access_20261025"
    execute = connection.execute
    if execute(text("SELECT name FROM data_versions WHERE name=:v"), {"v": version}).first():
        return
    from ..personal.access import access_of
    from .orm import SubscriberRow

    # SQLite julianday rounds sub-millisecond dates. Compare typed UTC datetimes
    # with the same policy as delivery, including frozen remnants at migration.
    rows = execute(
        select(SubscriberRow.telegram_id, SubscriberRow.paid_until, SubscriberRow.paused_at)
    )
    eligibility = []
    for uid, paid_until, paused_at in rows:
        old = SubscriberRow(telegram_id=uid, paid_until=paid_until, paused_at=paused_at)
        access = access_of(old, stamp, pause_max_days)
        eligible = not access.active(stamp) and access.frozen_seconds <= 0
        eligibility.append({"uid": uid, "eligible": eligible})
    # Explicit backfill: ALTER TABLE does not apply ORM Python defaults.
    if eligibility:
        execute(
            text("UPDATE subscribers SET trial_eligible=:eligible WHERE telegram_id=:uid"),
            eligibility,
        )
    # Existing pause remnants are preserved conservatively, never gifted over.
    for field in (
        "free_sends_used",
        "referral_credited",
        "referral_activated",
        "nudged_trial",
        "nudged_expiry",
        "nudged_winback",
        "channel_bonus_claimed",
    ):
        execute(text(f'UPDATE subscribers SET "{field}"=0 WHERE "{field}" IS NULL'))
    execute(text("INSERT INTO data_versions(name) VALUES (:v)"), {"v": version})


def _migrate_rental_terms(connection: Connection) -> None:
    """Reparse legacy Avito details once, within normal enrichment limits.

    Even filled values may be wrong: older extraction confused monthly deposit
    multiples with rubles and could read recommended cards. Mark those amounts
    unknown until the fixed parser has successfully read the actual conditions.
    """
    from sqlalchemy import text

    version = "rental_terms_v3"
    if connection.execute(text("SELECT 1 FROM data_versions WHERE name=:v"),
                          {"v": version}).first():
        return
    connection.execute(text(
        "UPDATE listings SET enriched_at=NULL, deposit_rub=NULL, commission_pct=NULL, "
        "commission_rub=NULL, deposit_due_rub=NULL, deposit_installments=0, "
        "upfront_payments='{}' WHERE source='avito'"
    ))
    connection.execute(text("INSERT INTO data_versions (name) VALUES (:v)"), {"v": version})


def _migrate_walking_metro(connection: Connection) -> None:
    """Discard legacy minutes whose walking mode/upper bound was not recorded.

    Saved user filters remain intact. Normal collection restores verified values;
    unknown durations cannot accidentally pass a maximum-time filter meanwhile.
    """
    from sqlalchemy import text

    version = "walking_metro_v1"
    if connection.execute(text("SELECT 1 FROM data_versions WHERE name=:v"),
                          {"v": version}).first():
        return
    connection.execute(text(
        "UPDATE listings SET metro_distance_min=NULL "
        "WHERE source IN ('avito', 'cian', 'yandex')"
    ))
    connection.execute(text("INSERT INTO data_versions (name) VALUES (:v)"), {"v": version})


def _backfill_flags(connection: Connection) -> None:
    """Flags added by ALTER TABLE arrive as NULL; make «not nudged» explicit."""
    from sqlalchemy import text

    connection.execute(
        text("UPDATE subscribers SET nudged_trial_soon=0 WHERE nudged_trial_soon IS NULL")
    )


def _migrate_add_missing_columns(connection: Connection) -> None:
    """Лёгкая миграция SQLite: добавить недостающие колонки в существующие таблицы.

    `create_all` не меняет существующие таблицы, поэтому при эволюции схемы старая
    БД ломалась («no such column»). Здесь сравниваем ORM-колонки с фактическими и
    делаем ALTER TABLE ADD COLUMN для недостающих — без пересоздания и потери данных.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.tables.values():
        if table.name not in existing_tables:
            continue
        have = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            col_type = column.type.compile(dialect=connection.dialect)
            connection.execute(
                text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}')
            )


def _to_row(listing: Listing) -> ListingRow:
    return ListingRow(
        fingerprint=listing.fingerprint,
        content_key=listing.content_key,
        source=listing.source.value,
        external_id=listing.external_id,
        url=listing.url,
        deal_type=listing.deal_type.value,
        property_type=listing.property_type.value,
        price=listing.price,
        area=listing.area,
        rooms=listing.rooms,
        city=listing.city,
        district=listing.district,
        address=listing.address,
        metro=listing.metro,
        metro_distance_min=listing.metro_distance_min,
        metro_line=listing.metro_line,
        metro_line_color=listing.metro_line_color,
        segment_key=listing.segment_key,
        lat=listing.lat,
        lon=listing.lon,
        floor=listing.floor,
        floors_total=listing.floors_total,
        building_material=listing.building_material,
        description=listing.description,
        commission_pct=listing.commission_pct,
        commission_rub=listing.commission_rub,
        deposit_due_rub=listing.deposit_due_rub,
        deposit_installments=listing.deposit_installments,
        upfront_payments=listing.upfront_payments,
        deposit_rub=listing.deposit_rub,
        meters_included=listing.meters_included,
        utilities_included=listing.utilities_included,
        utilities_rub=listing.utilities_rub,
        renovation=listing.renovation,
        has_furniture=listing.has_furniture,
        appeal=listing.appeal,
        best_photos=listing.best_photos,
        flagged=listing.flagged,
        analyzed_at=listing.analyzed_at,
        enriched_at=listing.enriched_at,
        photos=listing.photos,
        contact_hash=listing.contact_hash,
        published_at=listing.published_at,
        collected_at=listing.collected_at,
    )


def _row_to_listing(row: ListingRow) -> Listing:
    """Восстановить доменный Listing из строки БД (для рендера в персональном боте)."""
    return Listing(
        source=Source(row.source),
        external_id=row.external_id,
        url=row.url,
        deal_type=DealType(row.deal_type),
        property_type=PropertyType(row.property_type),
        price=row.price,
        area=row.area,
        rooms=row.rooms,
        city=row.city,
        district=row.district,
        address=row.address,
        metro=row.metro,
        metro_distance_min=row.metro_distance_min,
        metro_line=row.metro_line,
        metro_line_color=row.metro_line_color,
        lat=row.lat,
        lon=row.lon,
        floor=row.floor,
        floors_total=row.floors_total,
        building_material=row.building_material,
        description=row.description,
        commission_pct=row.commission_pct,
        commission_rub=row.commission_rub,
        deposit_due_rub=row.deposit_due_rub,
        deposit_installments=bool(row.deposit_installments),
        upfront_payments=row.upfront_payments or {},
        deposit_rub=row.deposit_rub,
        meters_included=row.meters_included,
        utilities_included=row.utilities_included,
        utilities_rub=row.utilities_rub,
        renovation=row.renovation,
        has_furniture=row.has_furniture,
        appeal=row.appeal,
        best_photos=list(row.best_photos or []),
        flagged=bool(row.flagged),
        analyzed_at=row.analyzed_at,
        enriched_at=row.enriched_at,
        photos=list(row.photos or []),
        contact_hash=row.contact_hash,
        published_at=row.published_at,
        collected_at=row.collected_at,
    )
