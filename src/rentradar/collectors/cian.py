"""Коллектор Циан.

Использует внутренний JSON-API поиска Циан (`search-offers`). Запрос строится из
`SearchProfile`, ответы листаются постранично, каждый оффер заворачивается в
`RawListing` (с сохранением исходного payload), затем `normalize()` приводит его
к каноническому `Listing`.

⚠️ С датацентрового IP Циан отдаёт captcha-редирект (см. H4) — тогда `fetch`
поднимает `CollectorBlockedError`. На резидентных прокси/через Playwright (Глава 7)
тот же код работает без изменений.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ..config import SearchProfile
from ..models import (
    DealType,
    Listing,
    PropertyType,
    RawListing,
    Source,
)
from .base import HttpCollector

logger = logging.getLogger(__name__)

API_URL = "https://api.cian.ru/search-offers/v2/search-offers-desktop/"
# Страница для «прогрева» сессии: грузим её, чтобы Циан выдал cookies
# (_CIAN_GK и пр.), без которых прямой вызов API уходит на капчу.
PRIME_URL = "https://www.cian.ru/snyat-kvartiru/"

# Город → код региона Циан.
REGION_BY_CITY: dict[str, int] = {
    "москва": 1,
    "санкт-петербург": 2,
    "спб": 2,
}

# Кол-во комнат (наша модель) → код «room» Циан. 0 = студия → 9.
ROOM_CODE: dict[int, int] = {0: 9, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}

_API_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://www.cian.ru",
    "Referer": "https://www.cian.ru/",
}

# Браузерные заголовки для прогрева сессии (вид «настоящего» захода на сайт).
_PRIME_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


class CianCollector(HttpCollector):
    source_name = Source.CIAN.value

    def __init__(self, *, max_pages: int = 5, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._max_pages = max_pages

    # ── сбор ─────────────────────────────────────────────────────────────
    async def fetch(self, profile: SearchProfile, *, limit: int = 50) -> list[RawListing]:
        region = _region_for(profile.city)
        results: list[RawListing] = []
        # follow_redirects=True нужен для прогрева (сайт может редиректить);
        # на сам API-вызов редирект запрещаем явно — там это сигнал блокировки.
        async with self._client(follow_redirects=True) as client:
            await self._prime_session(client)
            for page in range(1, self._max_pages + 1):
                query = self.build_query(profile, region=region, page=page)
                data = await self._post_json(
                    client, API_URL, json=query, headers=dict(_API_HEADERS), follow_redirects=False
                )
                offers = _extract_offers(data)
                if not offers:
                    break
                for offer in offers:
                    raw = _wrap_offer(offer)
                    if raw is not None:
                        results.append(raw)
                    if len(results) >= limit:
                        return results
        return results

    async def _prime_session(self, client: object) -> None:
        """Прогреть сессию: зайти на страницу Циан, чтобы получить cookies.

        Ошибки прогрева не фатальны — просто пробуем API дальше (и если он
        заблокирован, поднимется CollectorBlockedError штатно).
        """
        try:
            await self._throttle()
            await client.get(PRIME_URL, headers=dict(_PRIME_HEADERS), timeout=self._timeout)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — прогрев best-effort
            logger.debug("session prime failed (non-fatal): %s", exc)

    @staticmethod
    def build_query(profile: SearchProfile, *, region: int, page: int) -> dict:
        """Собрать тело `jsonQuery` из профиля поиска."""
        jq: dict[str, object] = {
            "_type": "flatrent",
            "engine_version": {"type": "term", "value": 2},
            "region": {"type": "terms", "value": [region]},
            "page": {"type": "term", "value": page},
        }

        if profile.deal_type == DealType.LONG_RENT:
            jq["for_day"] = {"type": "term", "value": "!1"}  # исключить посуточную
        elif profile.deal_type == DealType.SHORT_RENT:
            jq["for_day"] = {"type": "term", "value": "1"}

        if profile.rooms:
            codes = sorted({ROOM_CODE[r] for r in profile.rooms if r in ROOM_CODE})
            if codes:
                jq["room"] = {"type": "terms", "value": codes}

        price_range: dict[str, int] = {}
        if profile.price_min is not None:
            price_range["gte"] = profile.price_min
        if profile.price_max is not None:
            price_range["lte"] = profile.price_max
        if price_range:
            jq["price"] = {"type": "range", "value": price_range}

        if profile.metro_only:
            jq["only_foot"] = {"type": "term", "value": "2"}

        return {"jsonQuery": jq}

    # ── нормализация ─────────────────────────────────────────────────────
    def normalize(self, raw: RawListing) -> Listing | None:
        offer = raw.payload
        try:
            price = _price(offer)
            if price is None:
                return None
            rooms, prop_type = _rooms_and_type(offer)
            geo = offer.get("geo") or {}
            metro_name, metro_min, line_color = _metro(geo)
            coords = geo.get("coordinates") or {}

            building = offer.get("building") or {}
            bt = offer.get("bargainTerms") or {}
            return Listing(
                source=Source.CIAN,
                external_id=raw.external_id,
                url=raw.url,
                deal_type=DealType.LONG_RENT,
                property_type=prop_type,
                price=price,
                area=_area(offer),
                rooms=rooms,
                city=_city(geo) or "Москва",
                district=_district(geo),
                address=geo.get("userInput"),
                metro=metro_name,
                metro_distance_min=metro_min,
                metro_line_color=line_color,
                lat=coords.get("lat"),
                lon=coords.get("lng"),
                floor=_int_or_none(offer.get("floorNumber")),
                floors_total=_int_or_none(building.get("floorsCount")),
                building_material=_material(building),
                description=offer.get("description"),
                commission_pct=_int_or_none(bt.get("clientFee")),
                deposit_rub=_int_or_none(bt.get("deposit")),
                meters_included=_meters_included(bt),
                photos=_photos(offer),
                contact_hash=_contact_hash(offer),
                published_at=_published_at(offer),
                collected_at=datetime.now(UTC).replace(tzinfo=None),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("normalize failed for %s: %s", raw.url, exc)
            return None


# ── helpers ──────────────────────────────────────────────────────────────
def _region_for(city: str) -> int:
    return REGION_BY_CITY.get(city.strip().lower(), 1)


def _extract_offers(data: dict) -> list[dict]:
    return (data.get("data") or {}).get("offersSerialized") or []


def _wrap_offer(offer: dict) -> RawListing | None:
    ext_id = offer.get("cianId") or offer.get("id")
    url = offer.get("fullUrl")
    if not ext_id or not url:
        return None
    return RawListing(source=Source.CIAN, external_id=str(ext_id), url=url, payload=offer)


def _price(offer: dict) -> int | None:
    bt = offer.get("bargainTerms") or {}
    value = bt.get("priceRur") or bt.get("price")
    if value is None:
        return None
    price = int(round(float(value)))
    return price if price > 0 else None


def _area(offer: dict) -> float | None:
    raw = offer.get("totalArea")
    if raw is None:
        return None
    try:
        area = float(str(raw).replace(",", "."))
    except ValueError:
        return None
    return area if area > 0 else None


def _rooms_and_type(offer: dict) -> tuple[int | None, PropertyType]:
    if offer.get("isStudio"):
        return 0, PropertyType.STUDIO
    rooms = offer.get("roomsCount")
    if isinstance(rooms, int):
        return rooms, PropertyType.FLAT
    return None, PropertyType.UNKNOWN


def _city(geo: dict) -> str | None:
    for part in geo.get("address") or []:
        if part.get("type") in {"location", "city"}:
            return part.get("fullName") or part.get("name")
    return None


def _district(geo: dict) -> str | None:
    address = geo.get("address") or []
    # Район точнее округа — ищем его в первую очередь, округ как fallback.
    for wanted in ({"district", "raion"}, {"okrug"}):
        for part in address:
            if part.get("type") in wanted:
                return part.get("fullName") or part.get("name")
    return None


def _metro(geo: dict) -> tuple[str | None, int | None, str | None]:
    undergrounds = geo.get("undergrounds") or []
    if not undergrounds:
        return None, None, None
    # Берём ближайшее пешком, иначе первое.
    walk = [u for u in undergrounds if u.get("transportType") == "walk"]
    chosen = (walk or undergrounds)[0]
    line = chosen.get("lineColor") or chosen.get("color")
    return chosen.get("name"), chosen.get("time"), line


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _meters_included(bt: dict) -> bool | None:
    """Счётчики включены в цену? Циан: flowMetersNotIncludedInPrice=True → НЕ включены."""
    ut = bt.get("utilitiesTerms")
    if not isinstance(ut, dict):
        return None
    flag = ut.get("flowMetersNotIncludedInPrice")
    if flag is None:
        return None
    return not bool(flag)


# Коды/имена материалов Циан → человекочитаемо.
_MATERIAL_RU = {
    "brick": "кирпич",
    "monolith": "монолит",
    "monolithBrick": "монолит-кирпич",
    "panel": "панель",
    "block": "блок",
    "wood": "дерево",
    "stalin": "сталинка",
    "old": "старый фонд",
}


def _material(building: dict) -> str | None:
    raw = building.get("materialType")
    if isinstance(raw, dict):
        raw = raw.get("name") or raw.get("type")
    if not isinstance(raw, str) or not raw:
        return None
    return _MATERIAL_RU.get(raw, raw)


def _photos(offer: dict) -> list[str]:
    photos = offer.get("photos") or []
    urls = [p.get("fullUrl") for p in photos if p.get("fullUrl")]
    return urls[:10]  # 10 — максимум фото в одном альбоме Telegram


def _contact_hash(offer: dict) -> str | None:
    import hashlib

    phones = offer.get("phones") or []
    if not phones:
        return None
    parts = [f"{p.get('countryCode', '')}{p.get('number', '')}" for p in phones]
    key = "|".join(sorted(parts))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] if key.strip("|") else None


def _published_at(offer: dict) -> datetime | None:
    ts = offer.get("addedTimestamp") or offer.get("creationTimestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, tz=UTC).replace(tzinfo=None)
    return None
