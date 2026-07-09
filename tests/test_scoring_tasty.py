"""Тесты движка TastyScore: сигналы, anti-skam guard, холодный старт."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rentradar.config import SearchProfile
from rentradar.interfaces import MarketStatsProvider
from rentradar.models import DealType, Listing, PropertyType, Source
from rentradar.scoring import TastyScoringEngine


class FakeMarket(MarketStatsProvider):
    """Управляемый источник медиан для изоляции тестов от БД.

    coarse_only=True имитирует ситуацию «данные есть только по городу+комнатам»
    (грубый сегмент) — ключ города+комнат содержит ровно один разделитель «|».
    """

    def __init__(
        self, price: float | None, ppm: float | None, sample: int, coarse_only: bool = False
    ) -> None:
        self._price = price
        self._ppm = ppm
        self._sample = sample
        self._coarse_only = coarse_only

    async def median_price(self, segment_key: str) -> float | None:
        return self._price

    async def median_price_per_m2(self, segment_key: str) -> float | None:
        return self._ppm

    async def sample_size(self, segment_key: str) -> int:
        if self._coarse_only:
            return self._sample if segment_key.count("|") == 1 else 0
        return self._sample


def _listing(**over: object) -> Listing:
    base = {
        "source": Source.CIAN,
        "external_id": "1",
        "url": "https://cian.ru/1",
        "deal_type": DealType.LONG_RENT,
        "property_type": PropertyType.FLAT,
        "price": 55_000,
        "area": 38.0,
        "rooms": 1,
        "city": "Москва",
        "district": "Хамовники",
        "metro": "Парк культуры",
        "metro_distance_min": 5,
        "photos": ["p.jpg"],
        "collected_at": datetime(2026, 6, 21, 12, 0, 0),
        "published_at": datetime.now(UTC).replace(tzinfo=None),
    }
    base.update(over)
    return Listing(**base)  # type: ignore[arg-type]


@pytest.fixture
def profile() -> SearchProfile:
    return SearchProfile(name="t", city="Москва")


async def test_deep_discount_boosts_score(profile: SearchProfile) -> None:
    # Цена 55k против медианы 95k → дисконт ~42% (выше FULL_DISCOUNT) → максимум.
    engine = TastyScoringEngine(FakeMarket(price=95_000, ppm=3_000, sample=20))
    score = await engine.score(_listing(), profile)

    assert score.is_publishable
    bm = next(s for s in score.signals if s.name == "below_market")
    assert bm.value > 0.9
    assert score.total > 70


async def test_near_market_scores_low(profile: SearchProfile) -> None:
    # Дисконт ~8% (в «мёртвой зоне» < 12%) → below_market = 0, итог невысокий.
    engine = TastyScoringEngine(FakeMarket(price=60_000, ppm=1_550, sample=20))
    score = await engine.score(_listing(price=55_000), profile)

    bm = next(s for s in score.signals if s.name == "below_market")
    assert bm.value == 0.0
    assert score.total < 50


async def test_coarse_market_damps_cheap_district(profile: SearchProfile) -> None:
    # Данные только по городу+комнатам (грубый сегмент). Дешёвый район не должен
    # выглядеть «недооценкой»: дисконт демпфируется престижностью района.
    engine = TastyScoringEngine(
        FakeMarket(price=90_000, ppm=2_500, sample=20, coarse_only=True)
    )
    cheap = await engine.score(_listing(district="Капотня", price=55_000), profile)
    central = await engine.score(_listing(district="Хамовники", price=55_000), profile)

    assert "coarse_market" in cheap.flags
    cheap_bm = next(s for s in cheap.signals if s.name == "below_market").value
    central_bm = next(s for s in central.signals if s.name == "below_market").value
    # Тот же дисконт, но в престижном районе сигнал выше, в дешёвом — задавлен.
    assert central_bm > cheap_bm
    assert cheap_bm < 0.2


async def test_anti_skam_rejects_extreme_discount(profile: SearchProfile) -> None:
    # Цена 20k против медианы 70k → дисконт ~71% > 55% → отсев как приманка.
    engine = TastyScoringEngine(FakeMarket(price=70_000, ppm=1_800, sample=20))
    score = await engine.score(_listing(price=20_000), profile)

    assert score.rejected
    assert "suspicious_discount" in score.flags


async def test_cold_start_neutral_when_no_market(profile: SearchProfile) -> None:
    # Мало наблюдений (< MIN_SAMPLE) → рыночные сигналы нейтральны + флаг.
    engine = TastyScoringEngine(FakeMarket(price=None, ppm=None, sample=1))
    score = await engine.score(_listing(), profile)

    assert score.is_publishable
    bm = next(s for s in score.signals if s.name == "below_market")
    assert bm.value == 0.5
    assert "no_market_price" in score.flags


async def test_location_signal_metro_distance(profile: SearchProfile) -> None:
    engine = TastyScoringEngine(FakeMarket(price=60_000, ppm=1_600, sample=20))
    near = await engine.score(_listing(metro_distance_min=5), profile)
    far = await engine.score(_listing(metro_distance_min=25), profile)

    near_loc = next(s for s in near.signals if s.name == "location").value
    far_loc = next(s for s in far.signals if s.name == "location").value
    assert near_loc > far_loc
    assert near_loc == pytest.approx(1.0)


async def test_freshness_decays_with_age(profile: SearchProfile) -> None:
    engine = TastyScoringEngine(FakeMarket(price=60_000, ppm=1_600, sample=20))
    now = datetime.now(UTC).replace(tzinfo=None)
    fresh = await engine.score(_listing(published_at=now), profile)
    old = await engine.score(_listing(published_at=now - timedelta(days=3)), profile)

    fresh_v = next(s for s in fresh.signals if s.name == "freshness").value
    old_v = next(s for s in old.signals if s.name == "freshness").value
    assert fresh_v > 0.9
    assert old_v < 0.2


async def test_condition_signal_from_appeal(profile: SearchProfile) -> None:
    engine = TastyScoringEngine(FakeMarket(price=60_000, ppm=1_600, sample=20))
    pretty = await engine.score(_listing(appeal=95), profile)
    ugly = await engine.score(_listing(appeal=20), profile)
    neutral = await engine.score(_listing(appeal=None), profile)

    assert next(s for s in pretty.signals if s.name == "condition").value == 0.95
    assert next(s for s in ugly.signals if s.name == "condition").value == 0.20
    # Нет оценки → нейтральные 0.5 + флаг.
    assert next(s for s in neutral.signals if s.name == "condition").value == 0.5
    assert "no_appeal" in neutral.flags
    # Красивая квартира заметно обгоняет убитую при прочих равных (~25% веса).
    assert pretty.total - ugly.total > 15


async def test_fraud_min_price_and_photos(profile: SearchProfile) -> None:
    engine = TastyScoringEngine(FakeMarket(price=60_000, ppm=1_600, sample=20))
    cheap = await engine.score(_listing(price=1_000), profile)
    no_photo = await engine.score(_listing(photos=[]), profile)

    assert cheap.rejected
    assert no_photo.rejected
