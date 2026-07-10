"""ORM-модели (SQLAlchemy 2.x declarative).

Таблицы:
- listings      — нормализованные объявления (история для медиан + дедуп).
- market_stats  — кэш рыночных медиан по сегментам (Глава 3).
- posted_log    — что и в какой канал уже опубликовано (идемпотентность).
- subscribers   — подписчики приватного канала (Глава 6).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ListingRow(Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_source_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    external_id: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(512))

    deal_type: Mapped[str] = mapped_column(String(16))
    property_type: Mapped[str] = mapped_column(String(16))

    price: Mapped[int] = mapped_column(Integer)
    area: Mapped[float | None] = mapped_column(Float, nullable=True)
    rooms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    city: Mapped[str] = mapped_column(String(128), index=True)
    district: Mapped[str | None] = mapped_column(String(128), nullable=True)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metro: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metro_distance_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metro_line_color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    segment_key: Mapped[str] = mapped_column(String(256), index=True)
    content_key: Mapped[str] = mapped_column(String(24), index=True)

    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    floor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floors_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    building_material: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    commission_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deposit_rub: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meters_included: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    utilities_rub: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Анализ фото (vision)
    renovation: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    has_furniture: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    appeal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_photos: Mapped[list] = mapped_column(JSON, default=list)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)  # скам-оверлей на фото
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # Дообогащение с детальной страницы площадки (Авито)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    photos: Mapped[list] = mapped_column(JSON, default=list)
    contact_hash: Mapped[str | None] = mapped_column(String(32), nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MarketStatRow(Base):
    __tablename__ = "market_stats"

    segment_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    median_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    median_price_per_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PostedRow(Base):
    __tablename__ = "posted_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(32), index=True)
    channel: Mapped[str] = mapped_column(String(128))
    posted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SubscriberRow(Base):
    """Подписчик персонального бота: триал-счётчик + статус оплаты."""

    __tablename__ = "subscribers"

    telegram_id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    free_sends_used: Mapped[int] = mapped_column(Integer, default=0)
    paid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # пауза (заморозка)
    referred_by: Mapped[int | None] = mapped_column(Integer, nullable=True)  # кто пригласил
    referral_credited: Mapped[bool] = mapped_column(Boolean, default=False)  # награда уже начислена
    # Lifecycle: какие напоминания уже отправлены (чтобы не спамить).
    nudged_trial: Mapped[bool] = mapped_column(Boolean, default=False)
    nudged_expiry: Mapped[bool] = mapped_column(Boolean, default=False)
    nudged_winback: Mapped[bool] = mapped_column(Boolean, default=False)
    channel_bonus_claimed: Mapped[bool] = mapped_column(Boolean, default=False)  # бонус за канал
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class UserFilterRow(Base):
    """Сохранённый фильтр пользователя (критерии + интервал опроса)."""

    __tablename__ = "user_filters"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(128), default="Мой поиск")
    city: Mapped[str] = mapped_column(String(128), default="Москва")

    rooms: Mapped[list] = mapped_column(JSON, default=list)
    price_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    area_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    area_max: Mapped[float | None] = mapped_column(Float, nullable=True)

    metros: Mapped[list] = mapped_column(JSON, default=list)
    districts: Mapped[list] = mapped_column(JSON, default=list)
    max_metro_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    renovation_min: Mapped[str | None] = mapped_column(String(16), nullable=True)
    no_commission: Mapped[bool] = mapped_column(Boolean, default=False)

    interval_min: Mapped[int] = mapped_column(Integer, default=30)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class UserSentRow(Base):
    """Пер-юзер журнал отправленных лотов — чтобы не слать одно и то же дважды."""

    __tablename__ = "user_sent"
    __table_args__ = (UniqueConstraint("user_id", "content_key", name="uq_user_content"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    content_key: Mapped[str] = mapped_column(String(24), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
