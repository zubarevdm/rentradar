"""Коллектор Авито.

У Авито нет открытого JSON-API, зато страница поиска — server-side HTML с
карточками `data-marker="item"` (id, цена, заголовок с комнатами/площадью/этажом,
фото). Парсим их через selectolax. Заголовок вида «2-к. квартира, 40 м², 2/2 эт.»
несёт структуру — её и разбираем в `normalize`.

⚠️ Антибот Авито режет по TLS-ФИНГЕРПРИНТУ, а не по IP: голый httpx (Python-TLS)
ловит 403 «Доступ ограничен: проблема с IP» даже с дата-центра, а `curl_cffi` с
имитацией браузерного TLS проходит с того же IP без прокси. Рабочий фингерпринт
Авито периодически банит, поэтому держим набор и ротируем (`_avito_fetch_html`).
При исчерпании всех профилей страница не приходит → `CollectorBlockedError`, и
остальные площадки продолжают.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import NamedTuple

from curl_cffi import requests as cffi_requests
from selectolax.parser import HTMLParser

from ..config import SearchProfile
from ..errors import CollectorBlockedError
from ..models import DealType, Listing, PropertyType, RawListing, Source
from .base import HttpCollector

logger = logging.getLogger(__name__)

BASE = "https://www.avito.ru"

# Профили TLS-имитации curl_cffi. Авито адаптивно банит фингерпринт по мере
# нагрузки, поэтому держим несколько и ротируем: начинаем с последнего рабочего,
# при блоке идём по кругу. Порядок — эмпирический (safari/свежий chrome живучее).
_IMPERSONATE_PROFILES = (
    "safari17_0",
    "chrome124",
    "chrome123",
    "safari15_5",
    "chrome110",
    "chrome131",
)


class _FingerprintRotator:
    """Помнит индекс последнего сработавшего профиля; выдаёт порядок перебора."""

    def __init__(self, profiles: tuple[str, ...]) -> None:
        self._profiles = profiles
        self._good = 0

    def order(self) -> list[str]:
        n = len(self._profiles)
        return [self._profiles[(self._good + i) % n] for i in range(n)]

    def mark_good(self, profile: str) -> None:
        self._good = self._profiles.index(profile)


_rotator = _FingerprintRotator(_IMPERSONATE_PROFILES)


def _blocking_get(
    url: str, impersonate: str, params: dict | None, proxy: str | None, timeout: float
) -> object:
    kwargs: dict = {"impersonate": impersonate, "timeout": timeout}
    if params:
        kwargs["params"] = params
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    return cffi_requests.get(url, **kwargs)


async def _avito_fetch_html(
    url: str,
    *,
    params: dict | None = None,
    proxy: str | None = None,
    timeout: float = 25.0,
) -> str | None:
    """GET страницы Авито через curl_cffi с ротацией TLS-фингерпринта.

    Начинаем с последнего рабочего профиля; если он теперь режется (403 или
    заглушка «доступ ограничен») — перебираем остальные. Возвращаем HTML первого
    прошедшего профиля или None, если заблокированы все (тогда caller поднимет
    CollectorBlockedError). curl_cffi синхронный — гоним в пуле потоков."""
    for imp in _rotator.order():
        try:
            resp = await asyncio.to_thread(_blocking_get, url, imp, params, proxy, timeout)
        except Exception as exc:  # noqa: BLE001 — сетевые/curl-ошибки: пробуем следующий
            logger.warning("avito curl_cffi[%s]: %s: %s", imp, type(exc).__name__, exc)
            continue
        blocked = resp.status_code != 200 or "доступ ограничен" in resp.text[:4000].lower()
        if not blocked:
            _rotator.mark_good(imp)
            return resp.text
    return None
# Полноразмерные фото (1280x960) в JSON страницы объявления — экранированы: \"1280x960\":\"URL\".
_FULL_PHOTO_RE = re.compile(r'\\"1280x960\\":\\"(https://[^\\"]+?)\\"')
# Залог/комиссия одной строкой в JSON: \"depositCommission\":\"залог 125000 ₽, без комиссии\".
# Значение с внутренними &nbsp; — берём лениво до закрывающей \".
_DEPOSIT_COMMISSION_RE = re.compile(r'\\"depositCommission\\":\\"(.*?)\\"')
_DEPOSIT_RUB_RE = re.compile(r"залог\s*([\d ]+)")
_COMMISSION_PCT_RE = re.compile(r"комисси\w*\s*(\d+)\s*%")
_COMMISSION_RUB_RE = re.compile(r"комисси\w*\s*([\d ]+)\s*₽")


class AvitoDetail(NamedTuple):
    """Данные со страницы объявления Авито (один запрос → фото + описание + условия)."""

    photos: list[str]  # полноразмер 1280x960
    deposit_rub: int | None
    commission_pct: int | None
    description: str | None
    meters_included: bool | None


def _parse_deposit_commission(raw: str, price: int | None) -> tuple[int | None, int | None]:
    """Из строки `depositCommission` вытащить залог (₽) и комиссию (%).

    Пример: «залог 125000 ₽, без комиссии» → (125000, 0). Комиссия у Авито бывает и в
    процентах, и в рублях — рубли переводим в % через цену (для строки «Заехать»)."""
    s = (
        raw.replace("\\u0026nbsp;", " ")
        .replace("&nbsp;", " ")
        .replace("\xa0", " ")
        .replace("\\u002F", "/")
        .lower()
    )
    deposit: int | None = None
    if "без залога" in s:
        deposit = 0
    elif m := _DEPOSIT_RUB_RE.search(s):
        deposit = int(m.group(1).replace(" ", ""))
    commission: int | None = None
    if "без комисси" in s:
        commission = 0
    elif mp := _COMMISSION_PCT_RE.search(s):
        commission = int(mp.group(1))
    elif (mr := _COMMISSION_RUB_RE.search(s)) and price:
        commission = round(int(mr.group(1).replace(" ", "")) / price * 100)
    return deposit, commission


async def fetch_detail(
    url: str,
    *,
    price: int | None = None,
    proxy: str | None = None,
    timeout: float = 25.0,
    limit: int = 10,
) -> AvitoDetail | None:
    """Со страницы объявления вытащить полноразмер-фото (1280x960), описание и
    условия въезда (залог/комиссия/счётчики).

    В выдаче Авито только мелкие превью и ни описания, ни условий — всё это лежит
    на детальной странице; один запрос отдаёт всё сразу. None — блокировка/ошибка
    сети (вызывающему стоит прекратить проход и подождать)."""
    html = await _avito_fetch_html(url, proxy=proxy, timeout=timeout)
    if html is None:
        logger.warning("avito detail: не открыл %s (все TLS-профили заблокированы)", url)
        return None
    return parse_detail(html, price=price, limit=limit)


def parse_detail(html: str, *, price: int | None = None, limit: int = 10) -> AvitoDetail:
    """Разобрать HTML детальной страницы (отделено от сети — тестируется на фикстурах)."""
    photos: list[str] = []
    for raw in _FULL_PHOTO_RE.findall(html):
        u = raw.replace("\\u002F", "/").replace("\\/", "/")
        if u not in photos:
            photos.append(u)
    deposit = commission = meters = None
    if dc := _DEPOSIT_COMMISSION_RE.search(html):
        deposit, commission = _parse_deposit_commission(dc.group(1), price)
        low = dc.group(1).lower()
        if "счетчик" in low or "счётчик" in low:
            meters = False  # счётчики сверху
        elif "жку включ" in low or "все включено" in low:
            meters = True
    desc_el = HTMLParser(html).css_first("[data-marker='item-view/item-description']")
    description = desc_el.text(separator="\n", strip=True) if desc_el else None
    return AvitoDetail(photos[:limit], deposit, commission, description or None, meters)


async def fetch_full_photos(
    url: str, *, proxy: str | None = None, timeout: float = 25.0, limit: int = 10
) -> list[str]:
    """Обёртка над fetch_detail для вызовов, которым нужны только фото."""
    detail = await fetch_detail(url, proxy=proxy, timeout=timeout, limit=limit)
    return detail.photos if detail else []

# Город → путь в URL Авито.
REGION_PATH: dict[str, str] = {
    "москва": "moskva",
    "санкт-петербург": "sankt-peterburg",
    "спб": "sankt-peterburg",
}

# Путь категории: квартиры → сдам → длительный срок. Суффикс-токен кодирует фильтр
# «длительный срок» — без него отдаётся лендинг без объявлений. s=104 — по дате.
_CATEGORY_PATH = "kvartiry/sdam/na_dlitelnyy_srok-ASgBAgICAkSSA8gQ8AeQUg"


class AvitoCollector(HttpCollector):
    source_name = Source.AVITO.value

    def __init__(self, *, max_pages: int = 3, min_delay: float = 4.0, **kwargs: object) -> None:
        # Темп помедленнее остальных — у Авито антибот злее.
        super().__init__(min_delay=min_delay, **kwargs)  # type: ignore[arg-type]
        self._max_pages = max_pages

    async def fetch(self, profile: SearchProfile, *, limit: int = 50) -> list[RawListing]:
        region = REGION_PATH.get(profile.city.strip().lower(), "moskva")
        results: list[RawListing] = []
        for page in range(1, self._max_pages + 1):
            await self._throttle()
            url = f"{BASE}/{region}/{_CATEGORY_PATH}"
            html = await _avito_fetch_html(
                url, params={"s": "104", "p": page}, proxy=self._proxy, timeout=self._timeout
            )
            if html is None:
                raise CollectorBlockedError(
                    f"Авито: все TLS-профили заблокированы для {url}"
                )
            cards = _parse_cards(html)
            if cards is None:
                raise CollectorBlockedError(
                    f"Авито не отдал карточки (вероятно антибот/капча) для {url}"
                )
            if not cards:
                break
            for card in cards:
                raw = _wrap_card(card)
                if raw is not None:
                    results.append(raw)
                if len(results) >= limit:
                    return results
        return results

    def normalize(self, raw: RawListing) -> Listing | None:
        c = raw.payload
        try:
            price = c.get("price")
            if not price or price <= 0:
                return None
            rooms, prop_type = _rooms_and_type(c.get("title", ""))
            floor, floors_total = _floors(c.get("title", ""))
            return Listing(
                source=Source.AVITO,
                external_id=raw.external_id,
                url=raw.url,
                deal_type=DealType.LONG_RENT,
                property_type=prop_type,
                price=price,
                area=_area(c.get("title", "")),
                rooms=rooms,
                city=c.get("city") or "Москва",
                district=None,
                address=c.get("address") or None,
                metro=c.get("metro"),
                metro_distance_min=c.get("metro_time_min"),
                floor=floor,
                floors_total=floors_total,
                photos=c.get("photos", [])[:10],
                published_at=None,  # на карточке точной даты нет; берём «сейчас»
                collected_at=datetime.now(UTC).replace(tzinfo=None),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("avito normalize failed for %s: %s", raw.url, exc)
            return None


# ── парсинг HTML ────────────────────────────────────────────────────────────
def _parse_cards(html: str) -> list[dict] | None:
    """Карточки страницы → список словарей. None — если страница заблокирована."""
    tree = HTMLParser(html)
    cards = tree.css("div[data-marker='item']")
    if not cards:
        # Нет карточек: блок (капча/файрвол) либо пустая выдача. Реальная выдача —
        # тяжёлая (сотни КБ); короткая/без каталога страница = антибот.
        low = html.lower()
        blocked = len(html) < 100_000 or any(
            m in low for m in ("доступ ограничен", "firewall", "подтвердите, что вы не робот")
        )
        return None if blocked else []
    out: list[dict] = []
    for c in cards:
        item = _extract_card(c)
        if item:
            out.append(item)
    return out


def _extract_card(c: object) -> dict | None:
    iid = c.attributes.get("data-item-id")  # type: ignore[attr-defined]
    title_el = c.css_first("[data-marker='item-title']")  # type: ignore[attr-defined]
    if not iid or title_el is None:
        return None
    street, house, metro, metro_min = _address_parts(
        c.css_first("[data-marker='item-address']")  # type: ignore[attr-defined]
    )
    # Промо-карусель наверху выдачи — без адреса. Пропускаем: их content_key
    # вырождается (нет адреса) → ломается дедуп, лоты дублируются в 2 поста.
    if not street and not house:
        return None
    href = title_el.attributes.get("href") or ""
    price_el = c.css_first("[itemprop='price']")  # type: ignore[attr-defined]
    price = _int_or_none(price_el.attributes.get("content")) if price_el else None
    return {
        "id": str(iid),
        "url": BASE + href.split("?")[0],  # отрезаем ?context=…
        "title": title_el.text(strip=True),
        "price": price,
        "address": ", ".join(p for p in (street, house) if p) or None,
        "metro": metro,
        "metro_time_min": metro_min,
        "photos": _card_photos(c),
        "city": None,
    }


def _card_photos(c: object) -> list[str]:
    """Фото из data-marker 'slider-image/image-<URL>' — реальные URL даже для
    карточек ниже сгиба, где <img src> заглушка (ленивая подгрузка)."""
    prefix = "slider-image/image-"
    photos: list[str] = []
    for el in c.css("*"):  # type: ignore[attr-defined]
        dm = el.attributes.get("data-marker") or ""
        if dm.startswith(prefix):
            url = dm[len(prefix) :]
            if url.startswith("http") and url not in photos:
                photos.append(url)
    if not photos:  # запасной путь — обычные img
        for img in c.css("img"):  # type: ignore[attr-defined]
            src = img.attributes.get("src") or ""
            if src.startswith("http") and src not in photos:
                photos.append(src)
    return photos


def _address_parts(
    addr_el: object,
) -> tuple[str | None, str | None, str | None, int | None]:
    """Из блока адреса достать (улица, дом, метро, минут до метро). Текст склеен
    как «улица,домМетро,N мин.» — вырезаем улицу/дом, из «минут» берём число
    (для «11–15 мин» — верхнюю границу), остаётся станция."""
    if addr_el is None:
        return None, None, None, None
    s = addr_el.css_first("[data-marker='street_link']")  # type: ignore[attr-defined]
    h = addr_el.css_first("[data-marker='house_link']")  # type: ignore[attr-defined]
    street = s.text(strip=True) if s else None
    house = h.text(strip=True) if h else None
    full = addr_el.text(strip=True)  # type: ignore[attr-defined]

    # Время в пути: «до 15 мин», «от 5 мин», «11–15 мин» — режем из строки, но
    # число сохраняем: это metro_distance_min для поста и сигнала location.
    m = re.search(r"(?:от|до)?\s*(\d+)(?:[–-](\d+))?\s*мин", full)
    minutes = int(m.group(2) or m.group(1)) if m else None
    core = full[: m.start()] if m else full
    for part in (street, house):
        if part:
            core = core.replace(part, "", 1)
    metro = core.strip(" ,") or None
    return street, house, metro, minutes if metro else None


def _wrap_card(card: dict) -> RawListing | None:
    if not card.get("id") or not card.get("url"):
        return None
    return RawListing(
        source=Source.AVITO, external_id=card["id"], url=card["url"], payload=card
    )


# ── разбор заголовка «2-к. квартира, 40 м², 2/2 эт.» ────────────────────────
def _rooms_and_type(title: str) -> tuple[int | None, PropertyType]:
    low = title.lower()
    if "студия" in low:
        return 0, PropertyType.STUDIO
    if "комната" in low:
        return None, PropertyType.ROOM
    m = re.search(r"(\d+)-к", low)
    if m:
        return int(m.group(1)), PropertyType.FLAT
    return None, PropertyType.UNKNOWN


def _area(title: str) -> float | None:
    m = re.search(r"([\d.,]+)\s*м²", title)
    if not m:
        return None
    try:
        area = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return area if area > 0 else None


def _floors(title: str) -> tuple[int | None, int | None]:
    m = re.search(r"(\d+)/(\d+)\s*эт", title)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (ValueError, TypeError):
        return None
