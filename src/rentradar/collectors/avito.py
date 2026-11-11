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
import html as html_lib
import json
import logging
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import NamedTuple

from curl_cffi import requests as cffi_requests
from selectolax.parser import HTMLParser

from ..config import SearchProfile
from ..errors import CollectorBlockedError
from ..models import DealType, Listing, PropertyType, RawListing, Source
from ..photos import detail_photos, largest_srcset
from . import avito_access
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
_blocked_until: dict[str, float] = {}
_request_locks: dict[str, asyncio.Lock] = {}
_block_cooldown_seconds = 12 * 60.0


def set_request_cooldown(seconds: float) -> None:
    """Use the existing configured source interval for all Avito callers."""
    global _block_cooldown_seconds
    _block_cooldown_seconds = max(1.0, seconds)


def _blocking_get(
    url: str, impersonate: str, params: dict | None, proxy: str | None, timeout: float,
    cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None,
) -> object:
    kwargs: dict = {"impersonate": impersonate, "timeout": timeout}
    if params:
        kwargs["params"] = params
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    if cookies:
        kwargs["cookies"] = cookies
    if headers:
        kwargs["headers"] = headers
    return cffi_requests.get(url, **kwargs)


async def _avito_fetch_html(
    url: str,
    *,
    params: dict | None = None,
    proxy: str | None = None,
    timeout: float = 25.0,
) -> str | None:
    """GET страницы Авито через curl_cffi с ротацией TLS-фингерпринта.

    Начинаем с последнего рабочего профиля. При ограничении частоты или странице
    проверки безопасности приостанавливаем все обращения к источнику; прочие
    ошибки допускают следующий профиль. Возвращаем HTML или None при отказе.
    curl_cffi синхронный — гоним в пуле потоков.
    """
    # Search, enrichment and per-message fallback share the same source limit.
    # Serialise the check and request: parallel recipients must not retry a ban.
    key = proxy or "direct"
    lock = _request_locks.setdefault(key, asyncio.Lock())
    async with lock:
        if time.monotonic() < _blocked_until.get(key, 0):
            return None
        return await _fetch_with_profiles(url, params=params, proxy=proxy, timeout=timeout)


async def _fetch_with_profiles(
    url: str, *, params: dict | None, proxy: str | None, timeout: float,
) -> str | None:
    access = avito_access.current()
    if access is not None and access.proxy == proxy:
        return await _fetch_with_access(access, url, params=params, timeout=timeout)
    key = proxy or "direct"
    for imp in _rotator.order():
        try:
            resp = await asyncio.to_thread(_blocking_get, url, imp, params, proxy, timeout)
        except Exception as exc:  # noqa: BLE001 — сетевые/curl-ошибки: пробуем следующий
            logger.warning("avito curl_cffi[%s]: %s: %s", imp, type(exc).__name__, exc)
            continue
        prefix = resp.text[:4000].lower()
        challenge = any(text in prefix for text in (
            "доступ ограничен", "проверка безопасности", "подтвердите, что вы не робот",
        ))
        if resp.status_code in (429, 439) or challenge:
            retry = (getattr(resp, "headers", {}) or {}).get("Retry-After", "")
            delay = max(_block_cooldown_seconds, float(retry) if str(retry).isdigit() else 0)
            _blocked_until[key] = time.monotonic() + delay
            logger.warning("avito: HTTP %s, пауза запросов %.0f секунд",
                           resp.status_code, delay)
            return None
        blocked = resp.status_code != 200
        if not blocked:
            _rotator.mark_good(imp)
            return resp.text
    _blocked_until[key] = time.monotonic() + _block_cooldown_seconds
    return None


def _block_kind(resp: object) -> str | None:
    """None — страница нормальная; "ip" — бан IP; "challenge" — проверка браузера/лимит."""
    prefix = resp.text[:4000].lower()
    if "проблема с ip" in prefix:
        return "ip"
    if resp.status_code in (429, 439) or any(text in prefix for text in (
        "доступ ограничен", "проверка безопасности", "подтвердите, что вы не робот",
    )):
        return "challenge"
    return None if resp.status_code == 200 else "challenge"


async def _fetch_with_access(
    access: avito_access.AvitoAccess, url: str, *, params: dict | None, timeout: float,
) -> str | None:
    """Мобильный прокси: curl_cffi с куками и UA профиля браузера.

    При блоке — прогрев/смена IP и повтор."""
    key = access.proxy or "direct"
    for attempt in range(3):
        try:
            resp = await asyncio.to_thread(
                _blocking_get, url, avito_access.IMPERSONATE, params, access.proxy, timeout,
                access.cookies, access.headers,
            )
        except Exception as exc:  # noqa: BLE001 — сеть/прокси (телефон переподключается)
            logger.warning("avito via proxy: %s: %s", type(exc).__name__, exc)
            break
        kind = _block_kind(resp)
        if kind is None:
            return resp.text
        logger.warning("avito: HTTP %s (%s), попытка %d", resp.status_code, kind, attempt + 1)
        # 1-й блок — прогрев (или смена IP, если IP); не помогло — сразу новый IP.
        if attempt == 2 or not await access.recover(ip_banned=kind == "ip", retry=attempt == 1):
            break
    _blocked_until[key] = time.monotonic() + _block_cooldown_seconds
    return None
# Полноразмерные фото (1280x960) в JSON страницы объявления — экранированы: \"1280x960\":\"URL\".
_FULL_PHOTO_RE = re.compile(r'\\"1280x960\\":\\"(https://[^\\"]+?)\\"')
# Залог/комиссия одной строкой в JSON: \"depositCommission\":\"залог 125000 ₽, без комиссии\".
# Значение с внутренними &nbsp; — берём лениво до закрывающей \".
_AMOUNT = r"\d+(?: \d{3})*(?:[.,]\d+)?"
_CURRENCY = r"(?:₽|руб\w*\.?|р\.|rub\b)"
_PAYMENT_LABEL = r"(?:залог|(?:страховой )?депозит|комисси\w*)"
_JSON_START = re.compile(r"[\{\[]")
_GROUP_CHARS = re.compile(r"""[\\"'\[\]{}]""")


def _terms_text(raw: str) -> str:
    # JSON strings, HTML entities and both types of non-breaking spaces occur
    # together in Avito's embedded state. Do not use unicode_escape on Cyrillic.
    raw = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), raw)
    raw = html_lib.unescape(html_lib.unescape(raw)).lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"\s*:\s*", " ", raw)).strip()


def _amount(raw: str) -> Decimal:
    return Decimal(raw.replace(" ", "").replace(",", "."))


def _payment_value(text: str, label: str, price: int | None) -> tuple[int | None, int | None]:
    """Return (rubles, percent); an unrecognised/ambiguous value stays unknown."""
    if re.search(r"\bбез " + label, text):
        return 0, None
    match = re.search(r"\b" + label + r"\s*(?:[-—=]\s*)?", text)
    if not match:
        return None, None
    value = re.split(
        _PAYMENT_LABEL + r"|по счетчикам|другие жку",
        text[match.end():], maxsplit=1,
    )[0]
    value = re.sub(r"^(?:в размере|равен|составляет)\s+", "", value).strip()
    if re.match(r"(?:нет|отсутствует|не требуется|без\b)", value):
        return 0, None
    # A range, a minimum or a negotiable sum is not an exact entry payment.
    if re.match(r"(?:от |до |около |не указан|уточн|по договоренности)", value):
        return None, None
    if re.match(_AMOUNT + r"\s*[–—-]\s*\d", value):
        return None, None
    number = re.match(r"(" + _AMOUNT + r")\s*", value)
    if number:
        amount = _amount(number[1])
        unit = value[number.end():]
        if unit.startswith("%"):
            if amount == int(amount):
                return None, int(amount)
            return (round(Decimal(price) * amount / 100), None) if price else (None, None)
        if re.match(_CURRENCY, unit):
            return int(amount) if amount == int(amount) else None, None
        if re.match(r"(?:месяц|мес\b|месячн\w* (?:платеж|аренд)|арендн\w* платеж)", unit):
            return (round(amount * price), None) if price else (None, None)
        # Zero and an unqualified integer in the labelled deposit field are
        # valid, but '2 платежа' must never be interpreted as two rubles.
        if (not unit or re.match(r"[,;.]|(?:в рассрочку|частями)\b", unit)) and (
            amount == 0 or "комисси" not in label
        ):
            return int(amount) if amount == int(amount) else None, None
        return None, None
    words = {"один": 1, "одного": 1, "одной": 1, "два": 2, "двух": 2,
             "три": 3, "трех": 3, "полтора": 1.5}
    monthly = re.match(
        r"(?:(один|одного|одной|два|двух|три|трех|полтора)\s+)?"
        r"(?:месячн\w* (?:платеж|аренд)|месяц\w*(?: аренд\w*)?)", value
    )
    if monthly and price:
        return round(price * words.get(monthly[1], 1)), None
    return None, None


def _rental_amounts(text: str, price: int | None) -> tuple[int | None, int | None, int | None]:
    text = _terms_text(text)
    deposit, deposit_pct = _payment_value(text, r"(?:залог\w*|(?:страховой )?депозит\w*)", price)
    if deposit_pct is not None and price:
        deposit = round(price * deposit_pct / 100)
    fixed, pct = _payment_value(text, r"комисси\w*", price)
    # Keep explicit zero in the existing percent field for compatibility.
    return deposit, 0 if fixed == 0 else pct, None if fixed == 0 else fixed


def _structured_terms(document: str) -> list[str]:
    """Read nested JSON state without evaluating JavaScript or sibling adverts."""
    texts: list[str] = []
    pairs = {"{": "}", "[": "]"}

    def group_ends(source: str, start: int, cache: dict[int, int | None]) -> int | None:
        """Конец JS-группы, открытой в start (None — не закрылась до конца текста).

        Скан с открывающей скобки заодно определяет концы всех вложенных групп,
        встреченных вне кавычек: дальше они идут так же, как шёл бы скан с них
        самих. Кэш делает разбор многомегабайтной страницы линейным, а не
        квадратичным (раньше одна карточка разбиралась минутами и вешала бота).
        """
        if start in cache:
            return cache[start]
        stack: list[tuple[str, int]] = []
        quote: str | None = None
        skip = -1
        for match in _GROUP_CHARS.finditer(source, start):
            offset = match.start()
            if offset == skip:  # символ после \ внутри строки
                continue
            char = match.group()
            if quote:
                if char == "\\":
                    skip = offset + 1
                elif char == quote:
                    quote = None
            elif char in ('"', "'"):
                quote = char
            elif char in pairs:
                stack.append((pairs[char], offset))
            elif stack and char == stack[-1][0]:
                cache[stack.pop()[1]] = offset + 1
                if not stack:
                    return offset + 1
        for _, opened in stack:
            cache[opened] = None
        return None

    def scalar(value: object) -> str:
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, list):
            return " ".join(scalar(v) for v in value)
        if isinstance(value, dict):
            content = next((value[k] for k in ("text", "value", "title", "label")
                            if k in value), "")
            return scalar(content) + " " + scalar(value.get("unit", ""))
        return ""

    def walk(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            label = next((value[k] for k in ("label", "title", "name") if k in value), "")
            content = next((value[k] for k in ("value", "values", "text") if k in value), None)
            if isinstance(label, str) and content is not None:
                texts.append(f"{label}: {scalar(content)} {scalar(value.get('unit', ''))}")
            fields = {"depositRub": ("Залог", "₽"), "deposit": ("Залог", ""),
                      "commissionRub": ("Комиссия", "₽"),
                      "commissionPercent": ("Комиссия", "%"),
                      "commissionPct": ("Комиссия", "%")}
            for key, item in value.items():
                if key == "depositCommission" and isinstance(item, str):
                    texts.append(item)
                elif key in fields:
                    title, unit = fields[key]
                    rendered = scalar(item).strip()
                    texts.append(f"{title}: {rendered} {unit}")
                elif not re.search(
                    r"recommend|similar|related|suggest|otheritems|catalog", key, re.I
                ):
                    walk(item)

    decoder = json.JSONDecoder()
    # Two layers cover JSON in script strings as well as direct application/json.
    decoded = html_lib.unescape(document)
    for _ in range(2):
        position = 0
        cache: dict[int, int | None] = {}
        while match := _JSON_START.search(decoded, position):
            start = match.start()
            end = group_ends(decoded, start, cache)
            if end is None:  # незакрытая группа не может быть валидным JSON
                position = start + 1
                continue
            # Декодируем только саму группу: ошибка raw_decode на полном документе
            # считает номер строки от его начала — O(n) на каждую ложную «{».
            try:
                value, size = decoder.raw_decode(decoded[start:end])
            except ValueError:
                position = end  # невалидный JS-объект пропускаем целиком с вложенными
                continue
            walk(value)
            position = start + size
        decoded = decoded.replace(r'\"', '"').replace(r"\\", "\\")
    return texts


class AvitoDetail(NamedTuple):
    """Данные со страницы объявления Авито (один запрос → фото + описание + условия)."""

    photos: list[str]  # полноразмер 1280x960
    deposit_rub: int | None
    commission_pct: int | None
    description: str | None
    meters_included: bool | None
    commission_rub: int | None = None
    utilities_rub: int | None = None
    deposit_due_rub: int | None = None
    deposit_installments: bool = False
    upfront_payments: dict[str, int | None] | None = None
    utilities_included: bool | None = None
    deposit_present: bool = True
    commission_present: bool = True


def _parse_deposit_commission(raw: str, price: int | None) -> tuple[int | None, int | None]:
    """Legacy percent-only helper; detail parsing preserves exact rubles."""
    deposit, pct, fixed = _rental_amounts(raw, price)
    if fixed is not None and price:
        pct = round(fixed / price * 100)
    return deposit, pct


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

    Выдача содержит краткие условия; детальная страница дополняет их описанием,
    порядком оплаты и полными фото. None — блокировка/ошибка
    сети (вызывающему стоит прекратить проход и подождать)."""
    html = await _avito_fetch_html(url, proxy=proxy, timeout=timeout)
    if html is None:
        logger.warning("avito detail: не открыл %s (отказ источника или пауза запросов)", url)
        return None
    # Страница — мегабайты HTML/JSON: разбор в потоке, чтобы не стопорить бота.
    return await asyncio.to_thread(_detail_from_html, html, url, price, limit)


def _detail_from_html(
    html: str, url: str, price: int | None, limit: int,
) -> AvitoDetail | None:
    tree = HTMLParser(html)
    title_node = tree.css_first("title")
    title = title_node.text(strip=True).lower() if title_node else ""
    blocked = any(marker in title for marker in (
        "доступ ограничен", "проверка безопасности", "капча", "captcha",
    ))
    detail_markers = bool(re.search(r"item-view|условия\s+аренды", html, re.I))
    if not detail_markers:
        # Some page variants expose rental terms only in application state.
        # The structured reader excludes recommendation subtrees.
        detail_markers = any(re.search(
            r"\b(?:залог\w*|депозит\w*|комисси\w*)\b", _terms_text(item)
        ) for item in _structured_terms(html))
    if blocked or not detail_markers:
        logger.warning("avito detail: получена страница без данных объявления %s", url)
        return None
    return parse_detail(html, price=price, limit=limit)


def parse_detail(html: str, *, price: int | None = None, limit: int = 10) -> AvitoDetail:
    """Разобрать HTML детальной страницы (отделено от сети — тестируется на фикстурах)."""
    photos = detail_photos(html, limit)
    if not photos:  # preserve support for escaped fragments without an enclosing object
        for raw in _FULL_PHOTO_RE.findall(html):
            url = raw.replace("\\u002F", "/").replace("\\/", "/")
            if url not in photos:
                photos.append(url)
    tree = HTMLParser(html)
    desc_el = tree.css_first("[data-marker='item-view/item-description']")
    description = desc_el.text(separator="\n", strip=True) if desc_el else None
    # Use the rental section first, including CSS-hidden/closed HTML blocks.
    # Remove scripts from the text view: recommendation JSON is not this advert.
    texts = []
    structured = _structured_terms(html)
    for node in tree.css("script, style, [data-marker*='recommend'], [data-marker*='similar']"):
        node.decompose()
    body = tree.body.text(separator=" ", strip=True) if tree.body else ""
    section = re.split(r"условия\s+аренды", body, maxsplit=1, flags=re.I)
    if len(section) == 2:
        texts.append(re.split(
            r"правила|о квартире|описание|похожие объявления|рекомендации",
            section[1], maxsplit=1, flags=re.I,
        )[0])
    for node in tree.css(
        "[data-marker*='item-view/item-params'], [data-marker*='rental'], "
        "[data-marker='item-specific-params']"
    ):
        texts.append(node.text(separator=" ", strip=True))
    texts.extend(structured)
    deposit = commission = commission_rub = None
    deposit_seen = commission_seen = False
    for source_text in [*texts, description or ""]:
        normalized = _terms_text(source_text)
        if not normalized:
            continue
        dep_label = bool(re.search(r"\b(?:залог\w*|депозит\w*)\b", normalized))
        com_label = bool(re.search(r"\bкомисси\w*\b", normalized))
        if (dep_label and not deposit_seen) or (com_label and not commission_seen):
            dep, pct, fixed = _rental_amounts(normalized, price)
            if dep_label and not deposit_seen:
                deposit, deposit_seen = dep, True
            if com_label and not commission_seen:
                commission, commission_rub, commission_seen = pct, fixed, True
    terms = _terms_text(" ".join(texts))
    meters = None
    utilities = None
    utilities_included = None
    for label, kind in (
        (r"(?:по счетчикам|счетчики)", "meters"),
        (r"(?:другие жку|жку|коммунальные платежи|коммунальные услуги)", "utilities"),
    ):
        utility_match = re.search(label + r"\s+([^.;]{0,85})", terms)
        if not utility_match:
            continue
        text = utility_match.group(1)
        included = bool(re.match(r"(?:включ|все включ|оплачивает собственник)", text))
        separate = bool(re.match(r"(?:не включ|оплачива\w* отдельно|отдельно|сверху)", text))
        if kind == "meters":
            meters = True if included else False if separate else None
        elif included:
            utilities_included = True
            utilities = 0
        elif separate:
            utilities_included = False
        if kind == "utilities" and (amount := re.match(
            r"(?:оплачива\w* отдельно\s*)?([\d ]+)\s*(?:₽|руб)", text
        )):
            utilities = int(amount.group(1).replace(" ", ""))
    # Legacy combined terms: counters explicitly mentioned as an extra.
    if meters is None and re.search(r"(?:\+|сверху)\s*счетчик|счетчики сверху", terms):
        meters = False
    if meters is None and re.search(r"жку включ\w* без счетчиков", terms):
        meters = False
    # Installments and mandatory entry fees are often spelled out in the description.
    terms += " " + _terms_text(description or "")
    # A deposit installment belongs to the deposit clause only. The former
    # cross-sentence search could take an unrelated entry fee as its first due.
    deposit_contexts = re.finditer(
        r"(?=\b(?:залог\w*|(?:страховой )?депозит\w*)\b([^.!?]{0,160}))", terms
    )
    deposit_clause = ""
    for context in deposit_contexts:
        clause = re.split(
            r"\b(?:комисси\w*|обязательн\w* сбор|плата за оформление|"
            r"уборка|страховка|жку|по счетчикам)\b",
            context.group(1), maxsplit=1,
        )[0]
        if not deposit_clause or re.search(
            r"рассроч|частями|разбить|разделить|при заезде|первый взнос|первый платеж|"
            r"(?:на|в) (?:\d+|два|три) (?:платеж|месяц)", clause,
        ):
            # The price may be in the structured block while the schedule is
            # in a separate description clause. Do not stop at the first label.
            deposit_clause = clause
    installments = bool(re.search(
        r"рассроч|частями|разбить|разделить|"
        r"(?:на|в) (?:\d+|два|три) (?:платеж|месяц)", deposit_clause
    ))
    due_match = re.search(
        r"(?:при заезде|сейчас|первый платеж|первый взнос)\s*"
        r"([\d ]+)\s*(?:₽|руб)", deposit_clause
    )
    due = int(due_match.group(1).replace(" ", "")) if due_match else None
    equal = re.search(
        r"(?:на|в) (\d+) (?:платеж|месяц).{0,35}равными", deposit_clause
    )
    if installments and due is None and equal and deposit is not None:
        count = int(equal.group(1))
        if count and deposit % count == 0:
            due = deposit // count
    extras: dict[str, int | None] = {}
    for match in re.finditer(
        r"(обязательный сбор|плата за оформление|уборка|страховка|жку)"
        r"\s+(?:при заезде\s+)([\d ]+)\s*(?:₽|руб)", terms
    ):
        if match.group(1) != "жку" or utilities != 0:
            extras[match.group(1)] = int(match.group(2).replace(" ", ""))
    for unknown in re.finditer(
        r"(обязательный сбор|плата за оформление|уборка|страховка|жку)"
        r"\s+при заезде\s+(?:не указан\w*|уточня\w*|по договоренности)", terms
    ):
        extras.setdefault(unknown.group(1), None)
    prepaid = re.search(
        r"оплата за (\d+) месяц\w* при заезде(?P<tail>[^.!?]{0,100})", terms
    )
    if prepaid and int(prepaid.group(1)) > 1:
        uncertain = re.search(
            r"можно|возможн|обсужда|уточня|по договоренн|при согласова",
            terms[max(0, prepaid.start() - 30):prepaid.end()],
        )
        extras["арендный аванс за следующие месяцы"] = (
            None if uncertain or price is None else (int(prepaid.group(1)) - 1) * price
        )
    else:
        last_month = re.search(
            r"(?:оплата|оплатить) за первый и последний месяц(?P<tail>[^.!?]{0,100})",
            terms,
        )
        if last_month:
            uncertain = re.search(
                r"можно|возможн|обсужда|уточня|по договоренн|при согласова",
                terms[max(0, last_month.start() - 30):last_month.end()],
            )
            extras["арендный аванс за последний месяц"] = (
                None if uncertain else price
            )
    return AvitoDetail(photos[:limit], deposit, commission, description or None, meters,
                       commission_rub, utilities, due, installments, extras, utilities_included,
                       deposit_seen, commission_seen)



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

    def __init__(
        self,
        *,
        max_pages: int = 2,
        min_delay: float = 6.0,
        min_interval_sec: float = 0.0,
        **kwargs: object,
    ) -> None:
        # Темп помедленнее остальных — у Авито антибот злее.
        super().__init__(min_delay=min_delay, **kwargs)  # type: ignore[arg-type]
        self._max_pages = max_pages
        # Авито дёргаем не чаще этого (рейт-лимит по IP), даже если общий сбор чаще.
        self._min_interval_sec = min_interval_sec
        self._last_fetch_at = 0.0  # monotonic-время последней попытки

    async def fetch(self, profile: SearchProfile, *, limit: int = 50) -> list[RawListing]:
        # Гейт частоты: если с прошлой попытки прошло мало времени — пропускаем этот
        # цикл (возвращаем пусто, не блок), чтобы не долбить Авито и не ловить 429.
        loop_now = asyncio.get_event_loop().time()
        if self._min_interval_sec and (loop_now - self._last_fetch_at) < self._min_interval_sec:
            return []
        self._last_fetch_at = loop_now

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
                    f"Авито не отдал страницу или действует пауза после блокировки: {url}"
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
            payments = {}
            if c.get("rental_terms") or c.get("description"):
                detail = parse_detail(
                    '<div data-marker="item-specific-params">'
                    + html_lib.escape(c.get("rental_terms") or "")
                    + '</div><div data-marker="item-view/item-description">'
                    + html_lib.escape(c.get("description") or "") + '</div>',
                    price=price,
                )
                payments = {
                    key: value for key, value in detail._asdict().items()
                    if key not in ("photos", "deposit_present", "commission_present")
                }
                if not detail.commission_present:
                    payments.pop("commission_pct")
                    payments.pop("commission_rub")
                if not detail.deposit_present:
                    for key in ("deposit_rub", "deposit_due_rub", "deposit_installments"):
                        payments.pop(key)
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
                metro_line_color=c.get("metro_color"),
                floor=floor,
                floors_total=floors_total,
                photos=c.get("photos", [])[:10],
                published_at=None,  # на карточке точной даты нет; берём «сейчас»
                collected_at=datetime.now(UTC).replace(tzinfo=None),
                **payments,
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
    addr_el = c.css_first("[data-marker='item-address']")  # type: ignore[attr-defined]
    street, house, metro, metro_min = _address_parts(addr_el)
    # Промо-карусель наверху выдачи — без адреса. Пропускаем: их content_key
    # вырождается (нет адреса) → ломается дедуп, лоты дублируются в 2 поста.
    if not street and not house:
        return None
    href = title_el.attributes.get("href") or ""
    price_el = c.css_first("[itemprop='price']")  # type: ignore[attr-defined]
    price = _int_or_none(price_el.attributes.get("content")) if price_el else None
    terms_el = c.css_first("[data-marker='item-specific-params']")  # type: ignore[attr-defined]
    desc_el = c.css_first(  # type: ignore[attr-defined]
        "[data-marker='item-description'], p[style*='--module-max-lines-size:4'], "
        "p[style*='--module-max-lines-size: 4']"
    )
    return {
        "id": str(iid),
        "url": BASE + href.split("?")[0],  # отрезаем ?context=…
        "title": title_el.text(strip=True),
        "price": price,
        "rental_terms": terms_el.text(separator=" ", strip=True) if terms_el else None,
        "description": desc_el.text(separator=" ", strip=True) if desc_el else None,
        "address": ", ".join(p for p in (street, house) if p) or None,
        "metro": metro,
        "metro_time_min": metro_min,
        "metro_color": _metro_line_color(addr_el),
        "photos": _card_photos(c),
        "city": None,
    }


# Цвет линии метро у Авито — фон кружка станции: <... style="background-color:#RRGGBB">.
_METRO_COLOR_RE = re.compile(r"background-color:\s*#([0-9a-fA-F]{6})")


def _metro_line_color(addr_el: object) -> str | None:
    """Hex цвета ветки метро из кружка станции в карточке Авито (первая станция)."""
    if addr_el is None:
        return None
    m = _METRO_COLOR_RE.search(addr_el.html or "")  # type: ignore[attr-defined]
    return f"#{m.group(1)}" if m else None


def _card_photos(c: object) -> list[str]:
    """Фото из data-marker 'slider-image/image-<URL>' — реальные URL даже для
    карточек ниже сгиба, где <img src> заглушка (ленивая подгрузка)."""
    prefix = "slider-image/image-"
    photos: list[str] = []
    for el in c.css("*"):  # type: ignore[attr-defined]
        dm = el.attributes.get("data-marker") or ""
        if dm.startswith(prefix):
            url = dm[len(prefix):]
            candidates = [largest_srcset(el.attributes.get("srcset") or "")]
            candidates += [largest_srcset(img.attributes.get("srcset") or "")
                           for img in el.css("source, img")]
            available = [item for item in candidates if item is not None]
            if available:
                _, url = max(available, key=lambda item: item[0])
            if url.startswith("http") and url not in photos:
                photos.append(url)
    if not photos:
        for img in c.css("img"):  # type: ignore[attr-defined]
            src = img.attributes.get("src") or ""
            best = largest_srcset(img.attributes.get("srcset") or "")
            if best:
                _, src = best
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
    full = addr_el.text(separator=" ", strip=True)  # type: ignore[attr-defined]

    # Время в пути: «до 15 мин», «от 5 мин», «11–15 мин» — режем из строки, но
    # число сохраняем: это metro_distance_min для поста и сигнала location.
    m = re.search(r"(?:от|до)?\s*(\d+)(?:\s*[–-]\s*(\d+))?\s*мин", full)
    minutes = int(m.group(2) or m.group(1)) if m else None
    # Only a confirmed walking duration can be compared with this filter.
    mode = full.lower()
    for node in addr_el.css("[aria-label], [title], [data-marker]"):  # type: ignore[attr-defined]
        mode += " " + " ".join((node.attributes.get(k) or "") for k in (
            "aria-label", "title", "data-marker",
        )).lower()
    walking = bool(re.search(r"пешком|пеший|walk|on.foot", mode))
    transport = bool(re.search(r"транспорт|автомобил|на машине|transport|drive|car\b", mode))
    if not walking or transport or (m and m[0].strip().startswith("от") and not m[2]):
        minutes = None
    core = full[: m.start()] if m else full
    for part in (street, house):
        if part:
            core = core.replace(part, "", 1)
    core = re.sub(r"пешком|на транспорте|на машине", "", core, flags=re.I)
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
