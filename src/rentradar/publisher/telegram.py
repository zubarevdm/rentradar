"""Публикатор в Telegram (aiogram, Bot API).

`render()` — чистое форматирование текста поста (тестируемо без сети).
`publish()`:
  • dry-run или нет бота → пишет пост в лог (для проверки маршрутизации без спама);
  • иначе шлёт в канал: при наличии фото — `send_photo` с подписью, иначе
    `send_message`, плюс инлайн-кнопка «Открыть объявление».
Ошибки Telegram (бот не админ, неверный канал, лимиты) логируются и не валят
конвейер — один проблемный пост не должен останавливать весь прогон.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)

from ..collectors.avito import fetch_detail
from ..config import Settings, load_emoji
from ..interfaces import Publisher
from ..models import Listing, ScoreBreakdown, ScoredListing, Source

logger = logging.getLogger(__name__)

# Лимит подписи к фото в Telegram — 1024 символа.
_CAPTION_LIMIT = 1024


class TelegramPublisher(Publisher):
    def __init__(
        self,
        *,
        dry_run: bool = True,
        bot: Bot | None = None,
        max_photos_personal: int = 7,
        max_photos_public: int = 5,
        emoji: dict | None = None,
        show_renovation: bool = False,
        cta_text: str = "",
        cta_url: str = "",
        avito_proxy: str = "",
    ) -> None:
        self._dry_run = dry_run
        self._bot = bot
        self._avito_proxy = avito_proxy or None  # прокси для дозагрузки фото Авито (VPS)
        # Кап фото по каналу (Telegram: альбом ≤ 10). Личка — больше, канал — меньше.
        self._max_personal = max(1, min(10, max_photos_personal))
        self._max_public = max(1, min(10, max_photos_public))
        # Карта кастом-эмодзи (slot/линия → id). Применяется только в личке.
        self._emoji = emoji or {}
        # Временно показывать строку оценки ремонта в посте (калибровка).
        self._show_renovation = show_renovation
        # CTA-ссылка в конце поста (на бота) — для пересылок друзьям.
        self._cta_text = cta_text
        self._cta_url = cta_url

    @classmethod
    def from_settings(cls, settings: Settings) -> TelegramPublisher:
        """Собрать паблишер из настроек. Без токена/при dry_run — режим лога."""
        emoji = load_emoji(settings.emoji_path)
        show_ren = settings.show_renovation_in_post
        if settings.dry_run or not settings.bot_token:
            return cls(
                dry_run=True,
                max_photos_personal=settings.max_photos_personal,
                max_photos_public=settings.max_photos_public,
                emoji=emoji,
                show_renovation=show_ren,
                cta_text=settings.cta_text,
                cta_url=settings.cta_url,
                avito_proxy=settings.avito_proxy,
            )
        bot = Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        return cls(
            dry_run=False,
            bot=bot,
            max_photos_personal=settings.max_photos_personal,
            max_photos_public=settings.max_photos_public,
            emoji=emoji,
            show_renovation=show_ren,
            cta_text=settings.cta_text,
            cta_url=settings.cta_url,
            avito_proxy=settings.avito_proxy,
        )

    def render(self, scored: ScoredListing, *, use_custom_emoji: bool = False) -> str:
        listing = scored.listing
        em = self._emoji if use_custom_emoji else {}
        lines: list[str] = []

        # 1) Локация: метро + время пешком.
        # В личке — кастом-бейдж линии метро; в канале — обычный 📍.
        marker = _line_circle(listing.metro_line_color, em) if use_custom_emoji else "📍"
        if listing.metro:
            loc = f"м. {listing.metro}"
            if listing.metro_distance_min is not None:
                loc += f" ({listing.metro_distance_min} мин пешком)"
            lines.append(f"{marker} {loc}")
        elif _clean_district(listing.district):
            lines.append(f"{marker} {_clean_district(listing.district)}")

        # 2) Тип | площадь | этаж
        head = [_kind(listing)]
        if listing.area:
            head.append(f"{listing.area:g} м²")
        if listing.floor and listing.floors_total:
            head.append(f"{listing.floor}/{listing.floors_total} этаж")
        lines.append(f"{_ce(em, 'home', '🏢')} " + " | ".join(head))

        lines.append("")  # пустая строка — отделяем «что» от «почём»

        # 3) Цена/мес (+ залог, если есть)
        dep_hint = " (+ залог)" if (listing.deposit_pct or listing.deposit_rub) else ""
        lines.append(f"{_ce(em, 'price', '💳')} <b>{_money(listing.price)} ₽</b> / мес.{dep_hint}")

        # 4) Сумма на заезд с расшифровкой
        total = listing.move_in_total
        if total is not None:
            lines.append(
                f"🔑 <b>Заехать: {_money(total)} ₽</b> ({_move_in_breakdown(listing)})"
            )

        # 4c) Оценка ремонта (временно, для калибровки — прячется флагом show_renovation)
        if self._show_renovation:
            ren_line = _renovation_line(listing)
            if ren_line:
                lines.append(ren_line)

        # 5) Ссылка на источник — сразу под ценовым блоком, выше хэштегов.
        # Значок площадки: кастом source_<name> в личке (у Авито — 🛍); в канале
        # кастомов нет → плоский 🌐 для всех площадок.
        src_icon = _ce(em, f"source_{listing.source.value}", _ce(em, "link", "🌐"))
        lines.append(f'{src_icon} <a href="{listing.url}">Открыть объявление на источнике</a>')

        # 6) Хэштеги — только в канале (навигация по постам; в личке не нужны)
        if not use_custom_emoji:
            tags = _hashtags(listing)
            if tags:
                lines.append("")
                lines.append(" ".join(tags))

        # 7) CTA на бота — чтобы при пересылке другу легко перейти в FlatLikeThat
        if self._cta_url:
            lines.append("")
            lines.append(f'👉 <a href="{self._cta_url}">{self._cta_text}</a>')

        return "\n".join(lines)

    async def publish(
        self,
        scored: ScoredListing,
        *,
        channel: str,
        use_custom_emoji: bool = False,
    ) -> bool:
        """Опубликовать лот. Возвращает True только при реальной отправке.

        Каскад фолбэков на случай битых фото: альбом → одно фото → текст.
        Флуд-лимит (RetryAfter) обрабатывается ожиданием и повтором. `use_custom_emoji`
        включает кастом-эмодзи (только для личных чатов, не для канала).
        """
        # В личке больше фото (5-7), в канале меньше (3-5).
        cap = self._max_personal if use_custom_emoji else self._max_public
        # Avito: если лот ещё не дообогащён (enrich-джоб не успел), тянем детальную
        # страницу здесь — оттуда полноразмер-фото (1280x960) и залог/комиссия.
        # Патчим лот ДО render(), чтобы в посте появилась строка «Заехать».
        # Обогащённый лот уже несёт всё это в БД. В dry-run сеть не трогаем.
        avito_full: list[str] | None = None
        if (
            not self._dry_run
            and self._bot is not None
            and scored.listing.source == Source.AVITO
            and scored.listing.photos
            and scored.listing.enriched_at is None
        ):
            detail = await fetch_detail(
                scored.listing.url, price=scored.listing.price,
                proxy=self._avito_proxy, limit=cap,
            )
            if detail is not None:
                avito_full = detail.photos
                patch: dict = {}
                if scored.listing.deposit_rub is None and detail.deposit_rub is not None:
                    patch["deposit_rub"] = detail.deposit_rub
                if scored.listing.commission_pct is None and detail.commission_pct is not None:
                    patch["commission_pct"] = detail.commission_pct
                if patch:
                    scored = scored.model_copy(
                        update={"listing": scored.listing.model_copy(update=patch)}
                    )

        text = self.render(scored, use_custom_emoji=use_custom_emoji)
        if self._dry_run or self._bot is None:
            logger.info("[DRY-RUN] → %s\n%s", channel, text)
            return True

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Открыть объявление →", url=scored.listing.url)]
            ]
        )
        photos: list = _select_photos(scored.listing, cap)
        # Telegram не может стянуть фото Авито с CDN — скачиваем сами (наш IP
        # работает) и грузим байтами. Полноразмер уже получен выше.
        if photos and scored.listing.source == Source.AVITO:
            photos = await self._download_photos(avito_full or photos)
        # Уровни деградации: полный альбом → только первое фото → текст без фото.
        for level in (photos, photos[:1], []):
            if await self._try_send(channel, text, level, kb):
                return True
        logger.error("Не удалось отправить пост в %s (все фолбэки исчерпаны)", channel)
        return False

    async def _download_photos(self, urls: list[str]) -> list[BufferedInputFile]:
        """Скачать фото и завернуть в байты для загрузки (обход блокировки CDN)."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            )
        }
        out: list[BufferedInputFile] = []
        async with httpx.AsyncClient(
            timeout=20, headers=headers, follow_redirects=True, proxy=self._avito_proxy
        ) as cl:
            for i, url in enumerate(urls):
                try:
                    resp = await cl.get(url)
                    resp.raise_for_status()
                    out.append(BufferedInputFile(resp.content, filename=f"photo{i}.jpg"))
                except httpx.HTTPError as exc:
                    logger.warning("не скачал фото %s: %s", url, exc)
        return out

    async def _try_send(
        self, channel: str, text: str, photos: list, kb: InlineKeyboardMarkup
    ) -> bool:
        """Одна попытка отправки на заданном уровне фолбэка (с учётом флуда)."""
        assert self._bot is not None
        for _ in range(2):  # повтор после ожидания флуд-лимита
            try:
                if len(photos) >= 2 and len(text) <= _CAPTION_LIMIT:
                    media = [
                        InputMediaPhoto(media=url, caption=text, parse_mode=ParseMode.HTML)
                        if i == 0
                        else InputMediaPhoto(media=url)
                        for i, url in enumerate(photos)
                    ]
                    await self._bot.send_media_group(chat_id=channel, media=media)
                elif photos and len(text) <= _CAPTION_LIMIT:
                    await self._bot.send_photo(
                        chat_id=channel, photo=photos[0], caption=text, reply_markup=kb
                    )
                else:
                    await self._bot.send_message(
                        chat_id=channel, text=text, reply_markup=kb, disable_web_page_preview=False
                    )
                return True
            except TelegramRetryAfter as exc:
                wait = exc.retry_after + 1
                logger.warning("Флуд-лимит Telegram, ждём %s сек", wait)
                await asyncio.sleep(wait)
            except TelegramBadRequest as exc:
                # Часто — битая ссылка на фото: пусть сработает следующий фолбэк.
                logger.warning("Отклонено Telegram (фото=%d): %s", len(photos), exc)
                return False
            except TelegramAPIError as exc:
                logger.error("Ошибка Telegram при отправке в %s: %s", channel, exc)
                return False
        return False

    async def close(self) -> None:
        """Закрыть HTTP-сессию бота (вызывать в finally оркестратора)."""
        if self._bot is not None:
            await self._bot.session.close()


def _money(value: float) -> str:
    return f"{round(value):,}".replace(",", " ")


def _select_photos(listing: Listing, max_photos: int) -> list[str]:
    """Кадры для поста: отобранные vision-моделью, если есть; иначе первые. Кап лимитом."""
    chosen = listing.best_photos or listing.photos
    return chosen[:max_photos]


def _move_in_breakdown(listing: Listing) -> str:
    """Расшифровка суммы на заезд: «1-й месяц + 100% залог, без комиссии»."""
    parts = "1-й месяц"
    if listing.deposit_pct:
        parts += f" + {listing.deposit_pct}% залог"
    elif listing.deposit_rub:
        parts += " + залог"
    if listing.commission_pct == 0:
        parts += ", без комиссии"
    elif listing.commission_pct:
        parts += f", комиссия {listing.commission_pct}%"
    if listing.meters_included is False:
        parts += " + счётчики"
    return parts


# Ремонт (значение Renovation) → подпись для поста.
_RENOVATION_RU = {
    "needs_repair": "требует ремонта",
    "soviet": "бабушкин",
    "simple": "косметический",
    "modern": "современный",
    "designer": "дизайнерский",
}


def _renovation_line(listing: Listing) -> str | None:
    """Временная строка оценки ремонта/красоты (для калибровки). None если нет данных."""
    ren = _RENOVATION_RU.get(listing.renovation or "")
    if not ren:
        return None
    line = f"🛋 Ремонт: {ren}"
    if listing.appeal is not None:
        line += f" · 👁 {listing.appeal}/100"
    return line


def _terms_inline(listing: Listing) -> str | None:
    """Строка условий «комиссия X% · залог Y%». Если сумму на заезд посчитать
    нельзя (нет залога), а счётчики сверху — добавляем «+ счётчики» сюда. Иначе
    «+ счётчики» уходит в строку «Заехать». Пустые поля опускаем → None."""
    parts: list[str] = []
    if listing.commission_pct is not None:
        parts.append(
            "без комиссии" if listing.commission_pct == 0 else f"комиссия {listing.commission_pct}%"
        )
    dep_pct = listing.deposit_pct
    if dep_pct is not None:
        parts.append("без залога" if dep_pct == 0 else f"залог {dep_pct}%")
    if listing.move_in_total is None and listing.meters_included is False:
        parts.append("+ счётчики")
    return " · ".join(parts) if parts else None


def _first_reason(score: ScoreBreakdown) -> str | None:
    reasons = _tasty_reasons(score)
    return reasons[0] if reasons else None


def _humanize_reason(reason: str | None) -> str | None:
    """«+18% к рынку» → «цена на 18% ниже рынка»; «+12% к ₽/м²» → «₽/м² на 12%
    ниже рынка». Остальное (свежесть и т.п.) — как есть."""
    if not reason:
        return None
    m = re.match(r"^\+(\d+)% к рынку$", reason)
    if m:
        return f"цена на {m.group(1)}% ниже рынка"
    m = re.match(r"^\+(\d+)% к ₽/м²$", reason)
    if m:
        return f"₽/м² на {m.group(1)}% ниже рынка"
    return reason


# Сигналы, которые объясняют «вкусность» (метро/район уже в строке 📍).
_REASON_SIGNALS = {"below_market", "price_per_m2", "freshness"}
# Подстроки-маркеры неинформативных деталей — такие в пост не выводим.
_SKIP_MARKERS = ("нет ", "неизвестн", "по рынку")


def _tasty_reasons(score: ScoreBreakdown) -> list[str]:
    """Собрать строку причин «вкусности» только из информативных сигналов."""
    reasons: list[str] = []
    for sig in score.signals:
        if sig.name not in _REASON_SIGNALS or not sig.detail:
            continue
        if any(marker in sig.detail.lower() for marker in _SKIP_MARKERS):
            continue
        reasons.append(sig.detail)
    return reasons


def _kind(listing: Listing) -> str:
    if listing.rooms == 0:
        return "Студия"
    if listing.rooms:
        return f"{listing.rooms}-комн. квартира"
    return "Квартира"


def _ce(emoji_map: dict, slot: str, fallback: str) -> str:
    """Обернуть иконку в кастом-эмодзи, если для слота задан id; иначе fallback."""
    eid = emoji_map.get(slot) if emoji_map else None
    if eid:
        return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'
    return fallback


# Палитра цветов линий: имя · кружок-эмодзи · приблизительный RGB.
_CIRCLES: list[tuple[str, str, tuple[int, int, int]]] = [
    ("red", "🔴", (230, 30, 30)),
    ("orange", "🟠", (240, 140, 30)),
    ("yellow", "🟡", (245, 210, 40)),
    ("green", "🟢", (60, 180, 70)),
    ("blue", "🔵", (40, 110, 230)),
    ("purple", "🟣", (150, 70, 200)),
    ("brown", "🟤", (150, 100, 60)),
    ("black", "⚫", (40, 40, 40)),
    ("white", "⚪", (235, 235, 235)),
]


# Официальные цвета линий метро Москвы (номер → RGB) — для сопоставления с данными.
_MSK_LINES: dict[str, tuple[int, int, int]] = {
    "1": (237, 27, 53),    # Сокольническая — красная
    "2": (68, 184, 92),    # Замоскворецкая — зелёная
    "3": (0, 120, 190),    # Арбатско-Покровская — синяя
    "4": (0, 191, 255),    # Филёвская — голубая
    "4a": (0, 191, 255),   # Филёвская (ветка)
    "5": (137, 78, 53),    # Кольцевая — коричневая
    "6": (245, 130, 32),   # Калужско-Рижская — оранжевая
    "7": (142, 71, 156),   # Таганско-Краснопресненская — фиолетовая
    "8": (255, 205, 28),   # Калининская — жёлтая
    "8a": (255, 205, 28),  # Солнцевская — жёлтая
    "9": (161, 162, 163),  # Серпуховско-Тимирязевская — серая
    "10": (154, 202, 60),  # Люблинско-Дмитровская — салатовая
    "11": (121, 205, 205), # БКЛ — бирюзовая
    "12": (161, 179, 212), # Бутовская — серо-голубая
    "14": (255, 168, 175), # МЦК — бело-красная (по данным светло-коралловая)
    "15": (222, 100, 161), # Некрасовская — розовая
}


def _nearest_line(r: int, g: int, b: int) -> str:
    return min(
        _MSK_LINES,
        key=lambda n: sum((c - v) ** 2 for c, v in zip(_MSK_LINES[n], (r, g, b), strict=True)),
    )


def _line_circle(hex_color: str | None, emoji_map: dict | None = None) -> str:
    """Hex цвета линии → значок линии. Если задан кастом-эмодзи линии (emoji_map
    ['lines'][номер]) — он; иначе стандартный цветной кружок. Нет цвета → 📍."""
    em = emoji_map or {}
    if not hex_color:
        return _ce(em, "metro", "📍")
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return _ce(em, "metro", "📍")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return _ce(em, "metro", "📍")

    # Стандартный цветной кружок — он же fallback внутри <tg-emoji>.
    circle = min(
        _CIRCLES, key=lambda it: (it[2][0] - r) ** 2 + (it[2][1] - g) ** 2 + (it[2][2] - b) ** 2
    )[1]
    lines = em.get("lines") or {}
    if lines:
        eid = lines.get(_nearest_line(r, g, b))
        if eid:
            return f'<tg-emoji emoji-id="{eid}">{circle}</tg-emoji>'
    return circle


def _clean_district(district: str | None) -> str | None:
    if not district:
        return None
    return district.replace("р-н ", "").strip()


def _tag(text: str) -> str:
    """Слово → хэштег: пробелы/дефисы → подчёркивание, убрать прочую пунктуацию."""
    out = []
    for ch in text.strip():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -—":
            out.append("_")
    cleaned = "_".join(filter(None, "".join(out).split("_")))
    return f"#{cleaned}" if cleaned else ""


def _hashtags(listing: Listing) -> list[str]:
    """Навигационные хэштеги для канала: комнатность · метро · район · цена.
    Без мусорных тегов (#аренда на каждом посте, источник) — они только засоряют."""
    tags: list[str] = []
    if listing.rooms == 0:
        tags.append("#студия")
    elif listing.rooms:
        tags.append(f"#{listing.rooms}комн")
    if listing.metro:
        tags.append(_tag(listing.metro))
    district = _clean_district(listing.district)
    if district:
        tags.append(_tag(district))
    bucket = (listing.price + 9999) // 10000 * 10  # вверх до десятков тысяч
    tags.append(f"#до{bucket}к")
    return [t for t in tags if t]
