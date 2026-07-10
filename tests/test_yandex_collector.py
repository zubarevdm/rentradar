"""Тесты коллектора Яндекс Недвижимости: парсинг INITIAL_STATE, нормализация, блок."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from rentradar.collectors.yandex import YandexCollector, _extract_offers, _wrap_offer
from rentradar.config import SearchProfile
from rentradar.errors import CollectorBlockedError
from rentradar.models import PropertyType, Source

FIXTURE = Path(__file__).parent / "fixtures" / "yandex_page.html"


@pytest.fixture
def page_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def collector() -> YandexCollector:
    return YandexCollector()


# ── парсинг встроенного состояния ──────────────────────────────────────────
def test_extract_offers_from_html(page_html: str) -> None:
    offers = _extract_offers(page_html)
    assert offers is not None
    assert len(offers) == 3


def test_extract_returns_none_on_captcha() -> None:
    assert _extract_offers("<html>... showcaptcha ...</html>") is None


def test_extract_returns_none_without_state() -> None:
    assert _extract_offers("<html><body>no state here</body></html>") is None


# ── билдер запроса ─────────────────────────────────────────────────────────
def test_build_query() -> None:
    profile = SearchProfile(
        name="t", city="Москва", rooms=[0, 1], price_min=30000, price_max=70000
    )
    url, params = YandexCollector.build_query(profile, region="moskva", page=2)
    assert url.endswith("/moskva/snyat/kvartira/")
    d = dict(params)
    assert d["priceMin"] == "30000"
    assert d["priceMax"] == "70000"
    assert d["page"] == "2"
    assert ("sort", "DATE_DESC") in params
    rooms = [v for k, v in params if k == "roomsTotal"]
    assert "STUDIO" in rooms and "1" in rooms  # студия (0) → STUDIO


# ── нормализация ───────────────────────────────────────────────────────────
def test_normalize_flat(collector: YandexCollector, page_html: str) -> None:
    offers = _extract_offers(page_html)
    listing = collector.normalize(_wrap_offer(offers[0]))

    assert listing is not None
    assert listing.source is Source.YANDEX
    assert listing.external_id == "111"
    assert listing.url == "https://realty.yandex.ru/offer/111"
    assert listing.price == 55000
    assert listing.area == 38.0
    assert listing.rooms == 1
    assert listing.property_type is PropertyType.FLAT
    assert listing.floor == 5
    assert listing.floors_total == 12
    assert listing.building_material == "кирпич"
    assert listing.metro == "Парк культуры"
    assert listing.metro_distance_min == 7  # пешком → время учитываем
    assert listing.district == "Хамовники"
    assert listing.photos == [
        "https://avatars.mds.yandex.net/a/main",
        "https://avatars.mds.yandex.net/b/main",
    ]
    assert listing.published_at is not None


def test_normalize_studio_and_transport_metro(
    collector: YandexCollector, page_html: str
) -> None:
    offers = _extract_offers(page_html)
    listing = collector.normalize(_wrap_offer(offers[1]))

    assert listing is not None
    assert listing.rooms == 0
    assert listing.property_type is PropertyType.STUDIO
    assert listing.metro == "Павелецкая"
    # Метро на транспорте → время не указываем (вводит в заблуждение).
    assert listing.metro_distance_min is None


def test_daily_rent_is_filtered(collector: YandexCollector, page_html: str) -> None:
    offers = _extract_offers(page_html)
    # Третий оффер — посуточная аренда (PER_DAY) → нормализатор отсекает.
    assert collector.normalize(_wrap_offer(offers[2])) is None


# ── живой путь и детект блока ──────────────────────────────────────────────
async def test_fetch_parses_page(
    monkeypatch: pytest.MonkeyPatch, page_html: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return httpx.Response(200, text="<html>window.INITIAL_STATE = {\"search\":{}};</html>")
        return httpx.Response(200, text=page_html)

    collector = YandexCollector(min_delay=0.0)
    monkeypatch.setattr(
        collector,
        "_client",
        lambda *, follow_redirects=True: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    profile = SearchProfile(name="t", city="Москва")
    raws = await collector.fetch(profile)
    assert len(raws) == 3
    assert all(collector.normalize(r) for r in raws[:2])


async def test_fetch_raises_on_captcha(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>... showcaptcha please ...</html>")

    collector = YandexCollector(min_delay=0.0)
    monkeypatch.setattr(
        collector,
        "_client",
        lambda *, follow_redirects=True: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    with pytest.raises(CollectorBlockedError):
        await collector.fetch(SearchProfile(name="t", city="Москва"))


def test_studio_lowercase_key() -> None:
    # Яндекс отдаёт ключ студии в разном регистре — оба должны стать Студией (rooms=0).
    from rentradar.collectors.yandex import _rooms_and_type

    assert _rooms_and_type({"roomsTotalKey": "studio"}) == (0, PropertyType.STUDIO)
    assert _rooms_and_type({"roomsTotalKey": "STUDIO"}) == (0, PropertyType.STUDIO)
    assert _rooms_and_type({"studio": True}) == (0, PropertyType.STUDIO)
    assert _rooms_and_type({"roomsTotalKey": "2", "roomsTotal": 2}) == (2, PropertyType.FLAT)
