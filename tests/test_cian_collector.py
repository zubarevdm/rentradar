"""Тесты коллектора Циан: билдер запроса, нормализация офферов, детект блока."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from rentradar.collectors.cian import CianCollector, _extract_offers, _wrap_offer
from rentradar.config import SearchProfile
from rentradar.errors import CollectorBlockedError
from rentradar.models import DealType, PropertyType, Source

FIXTURE = Path(__file__).parent / "fixtures" / "cian_search_sample.json"


@pytest.fixture
def cian_data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def collector() -> CianCollector:
    return CianCollector()


# ── билдер запроса ────────────────────────────────────────────────────────
def test_build_query_basic() -> None:
    profile = SearchProfile(
        name="t",
        city="Москва",
        deal_type=DealType.LONG_RENT,
        rooms=[0, 1],
        price_min=30000,
        price_max=70000,
        metro_only=True,
    )
    q = CianCollector.build_query(profile, region=1, page=2)["jsonQuery"]

    assert q["_type"] == "flatrent"
    assert q["region"] == {"type": "terms", "value": [1]}
    assert q["page"] == {"type": "term", "value": 2}
    assert q["for_day"] == {"type": "term", "value": "!1"}
    assert q["price"] == {"type": "range", "value": {"gte": 30000, "lte": 70000}}
    # студия (0) → код 9, 1-комн → 1
    assert q["room"] == {"type": "terms", "value": [1, 9]}
    assert "only_foot" in q


def test_build_query_omits_empty_filters() -> None:
    profile = SearchProfile(name="t", city="Москва")
    q = CianCollector.build_query(profile, region=1, page=1)["jsonQuery"]
    assert "price" not in q
    assert "room" not in q
    assert "only_foot" not in q


# ── нормализация ──────────────────────────────────────────────────────────
def test_normalize_flat(collector: CianCollector, cian_data: dict) -> None:
    offers = _extract_offers(cian_data)
    raw = _wrap_offer(offers[0])
    assert raw is not None
    listing = collector.normalize(raw)

    assert listing is not None
    assert listing.source is Source.CIAN
    assert listing.external_id == "311111111"
    assert listing.price == 55000
    assert listing.area == 38.5
    assert listing.rooms == 1
    assert listing.property_type is PropertyType.FLAT
    assert listing.city == "Москва"
    assert listing.district == "Хамовники"
    assert listing.metro == "Парк культуры"
    assert listing.metro_distance_min == 7  # выбрано пешее метро, не транспортное
    assert listing.lat == pytest.approx(55.7345)
    assert listing.floor == 5
    assert listing.floors_total == 12
    assert listing.building_material == "кирпич"
    assert len(listing.photos) == 2
    assert listing.contact_hash is not None
    assert listing.published_at is not None


def test_normalize_studio(collector: CianCollector, cian_data: dict) -> None:
    offers = _extract_offers(cian_data)
    raw = _wrap_offer(offers[1])
    assert raw is not None
    listing = collector.normalize(raw)

    assert listing is not None
    assert listing.rooms == 0
    assert listing.property_type is PropertyType.STUDIO
    assert listing.area == 24.0  # запятая как разделитель распарсилась
    assert listing.district == "Даниловский"


def test_offer_without_url_is_skipped(cian_data: dict) -> None:
    offers = _extract_offers(cian_data)
    # У третьего оффера нет fullUrl → _wrap_offer вернёт None.
    assert _wrap_offer(offers[2]) is None


# ── детект блокировки (H4) ────────────────────────────────────────────────
async def test_fetch_raises_on_captcha_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Редирект на капчу → CollectorBlockedError, без ретраев и трейсбека."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/cian-captcha/?redirect_url=..."})

    collector = CianCollector(min_delay=0.0)

    def fake_client(*, follow_redirects: bool = False) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(collector, "_client", fake_client)

    profile = SearchProfile(name="t", city="Москва")
    with pytest.raises(CollectorBlockedError):
        await collector.fetch(profile)


async def test_fetch_parses_offers(monkeypatch: pytest.MonkeyPatch, cian_data: dict) -> None:
    """Счастливый путь: мокаем API, получаем нормализуемые RawListing."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Вторую страницу отдаём пустой, чтобы пагинация остановилась.
        body = json.loads(request.content)
        page = body["jsonQuery"]["page"]["value"]
        if page == 1:
            return httpx.Response(200, json=cian_data)
        return httpx.Response(200, json={"data": {"offersSerialized": []}})

    collector = CianCollector(min_delay=0.0)
    monkeypatch.setattr(
        collector,
        "_client",
        lambda *, follow_redirects=False: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    profile = SearchProfile(name="t", city="Москва")
    raws = await collector.fetch(profile)
    # Третий оффер без url отсеивается на этапе wrap.
    assert len(raws) == 2
    listings = [collector.normalize(r) for r in raws]
    assert all(listing is not None for listing in listings)
