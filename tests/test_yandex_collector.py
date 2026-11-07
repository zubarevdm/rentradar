"""Тесты коллектора Яндекс Недвижимости: парсинг INITIAL_STATE, нормализация, блок."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from curl_cffi.requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout

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


def test_extract_distinguishes_empty_results_from_missing_offers() -> None:
    assert _extract_offers('window.INITIAL_STATE = {"search":{"offers":{"entities":[]}}};') == []
    assert _extract_offers('window.INITIAL_STATE = {"search":{}};') is None


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


def test_two_room_query_uses_category_with_same_price_range() -> None:
    profile = SearchProfile(name="t", city="Москва", rooms=[2], price_min=50000, price_max=150000)
    url, params = YandexCollector.build_query(profile, region="moskva", page=1)
    assert url.endswith("/moskva/snyat/kvartira/dvuhkomnatnaya/")
    assert dict(params) == {"sort": "DATE_DESC", "priceMin": "50000", "priceMax": "150000"}


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
            return httpx.Response(
                200, text='window.INITIAL_STATE = {"search":{"offers":{"entities":[]}}};',
            )
        return httpx.Response(200, text=page_html)

    collector = YandexCollector(min_delay=0.0)
    monkeypatch.setattr(
        collector,
        "_search_session",
        lambda proxy: httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
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
        "_search_session",
        lambda proxy: httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
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


# ── резервный прокси при капче ─────────────────────────────────────────────
def _mock(collector: YandexCollector, direct, backup, calls: list[str]) -> None:
    def factory(proxy: str | None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: wrapped(request, proxy)),
            trust_env=False,
        )

    def wrapped(request: httpx.Request, proxy: str | None) -> httpx.Response:
        if proxy is None:
            calls.append("direct")
            return direct(request)
        calls.append("backup")
        return backup(request)

    collector._search_session = factory  # type: ignore[method-assign, assignment]


def _captcha(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="<html>... showcaptcha please ...</html>")


async def test_captcha_falls_back_to_proxy_and_holds(page_html: str) -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page_html)

    calls: list[str] = []
    collector = YandexCollector(min_delay=0.0, max_pages=1, fallback_proxy="http://phone:1")
    _mock(collector, _captcha, ok, calls)
    profile = SearchProfile(name="t", city="Москва")

    assert len(await collector.fetch(profile)) == 3
    assert calls == ["direct", "backup"]
    # Пока держимся резерва — основной IP не трогаем.
    calls.clear()
    assert len(await collector.fetch(profile)) == 3
    assert calls == ["backup"]
    # Срок вышел — снова пробуем напрямую.
    collector._fallback_until = 0.0
    calls.clear()
    await collector.fetch(profile)
    assert calls[0] == "direct"


async def test_captcha_on_both_raises_blocked() -> None:
    calls: list[str] = []
    collector = YandexCollector(min_delay=0.0, max_pages=1, fallback_proxy="http://phone:1")
    _mock(collector, _captcha, _captcha, calls)
    with pytest.raises(CollectorBlockedError):
        await collector.fetch(SearchProfile(name="t", city="Москва"))


async def test_blocked_query_pauses_without_stopping_other_profile(page_html: str) -> None:
    def direct(request: httpx.Request) -> httpx.Response:
        if "dvuhkomnatnaya" in str(request.url):
            return httpx.Response(200, text=page_html)
        return _captcha(request)

    calls: list[str] = []
    collector = YandexCollector(min_delay=0, max_pages=1, fallback_proxy="http://phone:1")
    _mock(collector, direct, _captcha, calls)
    profile = SearchProfile(name="all", city="Москва")
    with pytest.raises(CollectorBlockedError):
        await collector.fetch(profile)
    assert len(calls) == 2
    with pytest.raises(CollectorBlockedError):
        await collector.fetch(profile)
    assert len(calls) == 2
    assert await collector.fetch(SearchProfile(name="two", city="Москва", rooms=[2]))
    assert len(calls) == 3


async def test_proxy_network_error_is_blocked_not_crash() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise ConnectionError("phone offline")

    calls: list[str] = []
    collector = YandexCollector(min_delay=0.0, max_pages=1, fallback_proxy="http://phone:1")
    _mock(collector, _captcha, boom, calls)
    with pytest.raises(
        CollectorBlockedError, match=r"основной маршрут: SmartCaptcha.*ConnectionError",
    ):
        await collector.fetch(SearchProfile(name="t", city="Москва"))


async def test_fallback_fetches_only_first_page(page_html: str) -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page_html)

    calls: list[str] = []
    collector = YandexCollector(min_delay=0.0, max_pages=3, fallback_proxy="http://phone:1")
    _mock(collector, _captcha, ok, calls)
    await collector.fetch(SearchProfile(name="t", city="Москва"), limit=100)
    assert calls == ["direct", "backup"]


async def test_later_captcha_keeps_first_page_and_pauses_pagination(page_html: str) -> None:
    def direct(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return _captcha(request)
        return httpx.Response(200, text=page_html)

    calls: list[str] = []
    collector = YandexCollector(min_delay=0.0, fallback_proxy="http://phone:1")
    _mock(collector, direct, _captcha, calls)

    assert len(await collector.fetch(SearchProfile(name="t", city="Москва"))) == 3
    assert calls == ["direct", "direct"]

    other_profile = SearchProfile(name="other", city="Москва", rooms=[2])
    assert len(await collector.fetch(other_profile)) == 3
    assert calls == ["direct", "direct", "direct"]

    collector._pagination_paused_until = 0.0
    assert len(await collector.fetch(other_profile)) == 3
    assert calls == ["direct", "direct", "direct", "direct", "direct"]


async def test_failed_preferred_proxy_retries_primary_immediately(page_html: str) -> None:
    def direct(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page_html)

    def failed_proxy(request: httpx.Request) -> httpx.Response:
        raise ChunkedEncodingError("incomplete chunked read")

    calls: list[str] = []
    collector = YandexCollector(min_delay=0.0, max_pages=1, fallback_proxy="http://phone:1")
    collector._fallback_until = float("inf")
    _mock(collector, direct, failed_proxy, calls)

    profile = SearchProfile(name="t", city="Москва")
    assert len(await collector.fetch(profile)) == 3
    assert calls == ["backup", "direct"]
    assert len(await collector.fetch(profile)) == 3
    assert calls == ["backup", "direct", "direct"]


async def test_transport_failure_on_primary_tries_proxy(page_html: str) -> None:
    def failed_direct(request: httpx.Request) -> httpx.Response:
        raise Timeout("timed out")

    calls: list[str] = []
    collector = YandexCollector(min_delay=0.0, fallback_proxy="http://phone:1")
    _mock(collector, failed_direct, lambda _: httpx.Response(200, text=page_html), calls)

    assert len(await collector.fetch(SearchProfile(name="t", city="Москва"))) == 3
    assert calls == ["direct", "backup"]
