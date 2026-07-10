"""Коллектор Яндекс Недвижимости.

В отличие от Циан, у Яндекса нет отдельного JSON-API: данные встроены прямо в
HTML страницы поиска как `window.INITIAL_STATE = {...}`, а офферы лежат по пути
`search.offers.entities`. Поэтому `fetch` грузит страницу поиска (она же —
прогрев сессии) и парсит встроенный JSON.

⚠️ При срабатывании антибота Яндекс отдаёт страницу с SmartCaptcha (или без
INITIAL_STATE) — тогда `fetch` поднимает `CollectorBlockedError`, и остальные
площадки продолжают работать.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from ..config import SearchProfile
from ..errors import CollectorBlockedError
from ..models import (
    DealType,
    Listing,
    PropertyType,
    RawListing,
    Source,
)
from .base import HttpCollector

logger = logging.getLogger(__name__)

BASE = "https://realty.yandex.ru"

# Город → slug в URL Яндекса.
REGION_SLUG: dict[str, str] = {
    "москва": "moskva",
    "санкт-петербург": "sankt-peterburg",
    "спб": "sankt-peterburg",
}

# Кол-во комнат (наша модель) → значение параметра roomsTotal Яндекса.
ROOM_PARAM: dict[int, str] = {0: "STUDIO", 1: "1", 2: "2", 3: "3", 4: "PLUS_4"}

# Материал дома Яндекса → человекочитаемо.
_MATERIAL_RU = {
    "BRICK": "кирпич",
    "MONOLIT": "монолит",
    "MONOLIT_BRICK": "монолит-кирпич",
    "PANEL": "панель",
    "BLOCK": "блок",
    "WOOD": "дерево",
    "FERROCONCRETE": "ж/б",
}

_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Upgrade-Insecure-Requests": "1",
}


class YandexCollector(HttpCollector):
    source_name = Source.YANDEX.value

    def __init__(self, *, max_pages: int = 3, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._max_pages = max_pages

    async def fetch(self, profile: SearchProfile, *, limit: int = 50) -> list[RawListing]:
        region = REGION_SLUG.get(profile.city.strip().lower(), "moskva")
        results: list[RawListing] = []
        async with self._client(follow_redirects=True) as client:
            for page in range(1, self._max_pages + 1):
                url, params = self.build_query(profile, region=region, page=page)
                await self._throttle()
                resp = await client.get(
                    url, params=params, headers=dict(_HTML_HEADERS), timeout=self._timeout
                )  # type: ignore[arg-type]
                offers = _extract_offers(resp.text)
                if offers is None:
                    raise CollectorBlockedError(
                        f"Яндекс не отдал INITIAL_STATE (вероятно SmartCaptcha) для {url}"
                    )
                if not offers:
                    break
                for offer in offers:
                    raw = _wrap_offer(offer)
                    if raw is not None:
                        results.append(raw)
                    if len(results) >= limit:
                        return results
        return results

    @staticmethod
    def build_query(
        profile: SearchProfile, *, region: str, page: int
    ) -> tuple[str, list[tuple[str, str]]]:
        """Собрать URL страницы поиска и query-параметры из профиля."""
        url = f"{BASE}/{region}/snyat/kvartira/"
        params: list[tuple[str, str]] = [("sort", "DATE_DESC")]
        for r in sorted(set(profile.rooms)):
            if r in ROOM_PARAM:
                params.append(("roomsTotal", ROOM_PARAM[r]))
        if profile.price_min is not None:
            params.append(("priceMin", str(profile.price_min)))
        if profile.price_max is not None:
            params.append(("priceMax", str(profile.price_max)))
        if page > 1:
            params.append(("page", str(page)))
        return url, params

    def normalize(self, raw: RawListing) -> Listing | None:
        offer = raw.payload
        try:
            price_block = offer.get("price") or {}
            # Только помесячная аренда (отсекаем посуточную).
            if price_block.get("period") not in (None, "PER_MONTH"):
                return None
            price = _int_or_none(price_block.get("value"))
            if not price or price <= 0:
                return None

            rooms, prop_type = _rooms_and_type(offer)
            location = offer.get("location") or {}
            metro_name, metro_min = _metro(location)
            metro_block = location.get("metro") or {}
            line_color = metro_block.get("rgbColor") or (metro_block.get("lineColors") or [None])[0]
            point = location.get("point") or {}
            building = offer.get("building") or {}
            floors = offer.get("floorsOffered") or []

            return Listing(
                source=Source.YANDEX,
                external_id=raw.external_id,
                url=raw.url,
                deal_type=DealType.LONG_RENT,
                property_type=prop_type,
                price=price,
                area=_area(offer),
                rooms=rooms,
                city=_city(location),
                district=_district(location),
                address=location.get("geocoderAddress") or location.get("streetAddress"),
                metro=metro_name,
                metro_distance_min=metro_min,
                metro_line_color=line_color,
                lat=point.get("latitude"),
                lon=point.get("longitude"),
                floor=_int_or_none(floors[0]) if floors else None,
                floors_total=_int_or_none(offer.get("floorsTotal")),
                building_material=_MATERIAL_RU.get(building.get("buildingType", "")),
                description=offer.get("description"),
                commission_pct=_int_or_none(offer.get("agentFee")),
                deposit_rub=_int_or_none(offer.get("rentDeposit")),
                meters_included=_meters_included(offer),
                photos=_photos(offer),
                published_at=_published_at(offer),
                collected_at=datetime.now(UTC).replace(tzinfo=None),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("yandex normalize failed for %s: %s", raw.url, exc)
            return None


# ── парсинг INITIAL_STATE ───────────────────────────────────────────────────
def _extract_offers(html: str) -> list[dict] | None:
    """Достать офферы из HTML. None — если страница заблокирована/без данных."""
    if "showcaptcha" in html.lower():
        return None
    state = _parse_initial_state(html)
    if state is None:
        return None
    entities = (((state.get("search") or {}).get("offers")) or {}).get("entities")
    return entities if isinstance(entities, list) else []


def _parse_initial_state(html: str) -> dict | None:
    m = re.search(r"window\.INITIAL_STATE\s*=\s*", html)
    if not m:
        return None
    start = m.end()
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        c = html[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ── helpers ─────────────────────────────────────────────────────────────────
def _wrap_offer(offer: dict) -> RawListing | None:
    ext_id = offer.get("offerId")
    if not ext_id:
        return None
    url = f"{BASE}/offer/{ext_id}"
    return RawListing(source=Source.YANDEX, external_id=str(ext_id), url=url, payload=offer)


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _meters_included(offer: dict) -> bool | None:
    """Счётчики включены? utilitiesFee: 'included' → True, 'meter'/'not_included' → False."""
    uf = offer.get("utilitiesFee")
    if uf == "included":
        return True
    if uf in ("meter", "not_included"):
        return False
    return None


def _area(offer: dict) -> float | None:
    area = (offer.get("area") or {}).get("value")
    if isinstance(area, (int, float)) and area > 0:
        return float(area)
    return None


def _rooms_and_type(offer: dict) -> tuple[int | None, PropertyType]:
    # Яндекс отдаёт ключ студии в РАЗНОМ регистре ("STUDIO" и "studio") — сравниваем
    # без регистра, иначе студия попадает в UNKNOWN и рендерится как «Квартира».
    key = str(offer.get("roomsTotalKey") or "").upper()
    if key == "STUDIO" or offer.get("studio") is True:
        return 0, PropertyType.STUDIO
    rooms = offer.get("roomsTotal")
    if isinstance(rooms, int):
        return rooms, PropertyType.FLAT
    return None, PropertyType.UNKNOWN


def _city(location: dict) -> str:
    for part in (location.get("structuredAddress") or {}).get("component") or []:
        if part.get("regionType") == "CITY":
            return part.get("value") or "Москва"
    return "Москва"


def _district(location: dict) -> str | None:
    # Район у Яндекса есть не всегда; берём компонент DISTRICT/SUBLOCALITY если есть.
    for part in (location.get("structuredAddress") or {}).get("component") or []:
        if part.get("regionType") in {"DISTRICT", "SUBLOCALITY", "CITY_DISTRICT"}:
            return part.get("value")
    return None


def _metro(location: dict) -> tuple[str | None, int | None]:
    metro = location.get("metro") or {}
    name = metro.get("name")
    if not name:
        return None, None
    # Время считаем только для пешей доступности — иначе оно вводит в заблуждение.
    if metro.get("metroTransport") == "ON_FOOT":
        return name, _int_or_none(metro.get("timeToMetro") or metro.get("minTimeToMetro"))
    return name, None


def _photos(offer: dict) -> list[str]:
    for key in ("fullImages", "mainImages", "appMiddleImages"):
        images = offer.get(key)
        if images:
            urls = [_abs_url(u) for u in images if isinstance(u, str)]
            urls = [u for u in urls if u]
            if urls:
                return urls[:10]
    return []


def _abs_url(url: str) -> str:
    return f"https:{url}" if url.startswith("//") else url


def _published_at(offer: dict) -> datetime | None:
    raw = offer.get("creationDate") or offer.get("updateDate")
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(UTC).replace(tzinfo=None)
    except ValueError:
        return None
