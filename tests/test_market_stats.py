"""Тест пересчёта рыночных медиан из накопленных объявлений."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rentradar.models import DealType, Listing, PropertyType, Source
from rentradar.storage import SqlMarketStatsProvider, SqlStorage


def _listing(ext_id: str, price: int, area: float, collected_at: datetime | None = None) -> Listing:
    return Listing(
        source=Source.CIAN,
        external_id=ext_id,
        url=f"https://cian.ru/{ext_id}",
        deal_type=DealType.LONG_RENT,
        property_type=PropertyType.FLAT,
        price=price,
        area=area,
        rooms=1,
        city="Москва",
        district="Хамовники",
        photos=["p.jpg"],
        collected_at=collected_at or datetime(2026, 6, 21, 12, 0, 0),
    )


@pytest.fixture
async def storage(tmp_path):
    st = SqlStorage(f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}")
    await st.init()
    yield st
    await st.dispose()


async def test_recompute_median_for_segment(storage: SqlStorage) -> None:
    # Один сегмент (одинаковые район/комнаты/бакет площади), цены 50/60/70k.
    await storage.upsert_listings(
        [_listing("1", 50_000, 40.0), _listing("2", 60_000, 40.0), _listing("3", 70_000, 40.0)]
    )
    segments = await storage.recompute_market_stats()
    # 3 уровня иерархии (точный, район+комнаты, город+комнаты) — все различны.
    assert segments == 3

    provider = SqlMarketStatsProvider(storage)
    sample = _listing("x", 60_000, 40.0)
    for seg in sample.segment_keys:  # на каждом уровне те же 3 наблюдения
        assert await provider.median_price(seg) == 60_000
        assert await provider.sample_size(seg) == 3
        # медиана ₽/м² = 60000/40 = 1500
        assert await provider.median_price_per_m2(seg) == pytest.approx(1500.0)


async def test_missing_segment_returns_none(storage: SqlStorage) -> None:
    provider = SqlMarketStatsProvider(storage)
    assert await provider.median_price("nope") is None
    assert await provider.sample_size("nope") == 0


async def test_window_excludes_old_listings(storage: SqlStorage) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    recent = _listing("r", 60_000, 40.0, collected_at=now - timedelta(days=5))
    old = _listing("o", 200_000, 40.0, collected_at=now - timedelta(days=200))  # старьё, дорогое
    await storage.upsert_listings([recent, old])

    provider = SqlMarketStatsProvider(storage)
    seg = recent.segment_key

    # Без окна — старый дорогой лот тянет медиану вверх (медиана 60k и 200k = 130k).
    await storage.recompute_market_stats()
    assert await provider.median_price(seg) == 130_000

    # С окном 90 дней — старый лот исключён, медиана = только свежий.
    await storage.recompute_market_stats(window_days=90)
    assert await provider.median_price(seg) == 60_000
    assert await provider.sample_size(seg) == 1


async def test_recollect_same_listing_changed_price(storage: SqlStorage) -> None:
    # Тот же лот (source+external_id) с изменившейся ценой не должен падать на
    # UNIQUE и не должен задваиваться.
    first = await storage.upsert_listings([_listing("1", 50_000, 40.0)])
    assert len(first) == 1
    again = await storage.upsert_listings([_listing("1", 55_000, 40.0)])  # цена другая
    assert again == []  # уже есть — не новый, без ошибки
    assert await storage.count_listings() == 1
