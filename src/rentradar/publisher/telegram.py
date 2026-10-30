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
import html
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)

from ..collectors.avito import fetch_detail
from ..config import Settings, load_emoji
from ..interfaces import Publisher
from ..models import Listing, ScoreBreakdown, ScoredListing, Source
from ..personal.navigation import listing_keyboard
from ..photos import image_dimensions, telegram_photo

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..storage.repository import SqlStorage

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
        cta_public_text: str = "",
        cta_public_url: str = "",
        avito_proxy: str = "",
        protect_bot: bool = False,
        protect_public: bool = False,
        storage: SqlStorage | None = None,
    ) -> None:
        self._dry_run = dry_run
        self._bot = bot
        self._storage = storage
        self._avito_proxy = avito_proxy or None  # прокси для дозагрузки фото Авито (VPS)
        # Кап фото по каналу (Telegram: альбом ≤ 10). Личка — больше, канал — меньше.
        self._max_personal = max(1, min(10, max_photos_personal))
        self._max_public = max(1, min(10, max_photos_public))
        # Карта кастом-эмодзи (slot/линия → id). Применяется только в личке.
        self._emoji = emoji or {}
        # Временно показывать строку оценки ремонта в посте (калибровка).
        self._show_renovation = show_renovation
        # CTA в конце поста: в канале ведём на бота, в личке — на публичный канал.
        self._cta_text = cta_text
        self._cta_url = cta_url
        self._cta_public_text = cta_public_text
        self._cta_public_url = cta_public_url
        # Запрет пересылки/сохранения (protect_content): в личке и/или в канале.
        self._protect_bot = protect_bot
        self._protect_public = protect_public

    @classmethod
    def from_settings(
        cls, settings: Settings, *, storage: SqlStorage | None = None,
    ) -> TelegramPublisher:
        """Собрать паблишер из настроек. Без токена/при dry_run — режим лога."""
        emoji = load_emoji(settings.emoji_path)
        show_ren = settings.show_renovation_in_post
        if settings.dry_run or not settings.bot_token:
            return cls(
                dry_run=True,
                storage=storage,
                max_photos_personal=settings.max_photos_personal,
                max_photos_public=settings.max_photos_public,
                emoji=emoji,
                show_renovation=show_ren,
                cta_text=settings.cta_text,
                cta_url=settings.cta_url,
                cta_public_text=settings.cta_public_text,
                cta_public_url=settings.cta_public_url,
                avito_proxy=settings.avito_proxy,
                protect_bot=settings.protect_content_bot,
                protect_public=settings.protect_content_public,
            )
        bot = Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        return cls(
            dry_run=False,
            storage=storage,
            bot=bot,
            max_photos_personal=settings.max_photos_personal,
            max_photos_public=settings.max_photos_public,
            emoji=emoji,
            show_renovation=show_ren,
            cta_text=settings.cta_text,
            cta_url=settings.cta_url,
            avito_proxy=settings.avito_proxy,
        )

    def render(
        self, scored: ScoredListing, *, use_custom_emoji: bool = False,
        rental_terms_unavailable: bool = False,
    ) -> str:
        listing = scored.listing
        em = self._emoji if use_custom_emoji else {}
        lines: list[str] = []

        # 1) Локация: метро + время пешком.
        # В личке — кастом-бейдж линии метро; в канале — обычный 📍.
        marker = _line_circle(listing.metro_line_color, em) if use_custom_emoji else "📍"
        if listing.metro:
            loc = f"м. {html.escape(listing.metro[:80])}"
            if listing.metro_distance_min is not None:
                loc += f" ({listing.metro_distance_min} мин пешком)"
            lines.append(f"{marker} {loc}")
        elif _clean_district(listing.district):
            lines.append(f"{marker} {html.escape((_clean_district(listing.district) or "")[:80])}")

        # 2) Тип | площадь | этаж
        head = [_kind(listing)]
        if listing.area:
            head.append(f"{listing.area:g} м²")
        if listing.floor and listing.floors_total:
            head.append(f"{listing.floor}/{listing.floors_total} этаж")
        lines.append(f"{_ce(em, 'home', '🏢')} " + " | ".join(head))

        lines.append("")  # пустая строка — отделяем «что» от «почём»

        # 3) Цена/мес + что доплачивать ежемесячно (счётчики / фикс. ЖКУ ₽).
        # Залог сюда НЕ пишем — он только в строке «Заехать» (разовый, не ежемесячный).
        util = _utilities_suffix(listing)
        util_str = f" {util}" if util else ""
        lines.append(
            f"{_ce(em, 'price', '💳')} <b>{_money(listing.price)} ₽</b> / мес.{util_str}"
        )

        # 4) Сумма на заезд с расшифровкой
        lines.append(_move_in_line(listing, unavailable=rental_terms_unavailable))

        # 4c) Оценка ремонта (временно, для калибровки — прячется флагом show_renovation)
        if self._show_renovation:
            ren_line = _renovation_line(listing)
            if ren_line:
                lines.append(ren_line)

        # 5) Ссылка на источник — сразу под ценовым блоком, выше хэштегов.
        # Значок площадки: кастом source_<name> в личке (у Авито — 🛍); в канале
        # кастомов нет → плоский 🌐 для всех площадок.
        src_icon = _ce(em, f"source_{listing.source.value}", _ce(em, "link", "🌐"))
        source_url = html.escape(listing.url, quote=True)
        lines.append(f'{src_icon} <a href="{source_url}">Открыть объявление на источнике</a>')

        # 6) Хэштеги — только в канале (навигация по постам; в личке не нужны)
        if not use_custom_emoji:
            tags = _hashtags(listing)
            if tags:
                lines.append("")
                lines.append(" ".join(tags))

        # 7) CTA: в канале → на бота (читатель настроит свой поиск), в личке → на
        # публичный канал (подписчик увидит общий поток / перешлёт друзьям).
        cta_url = self._cta_public_url if use_custom_emoji else self._cta_url
        cta_text = self._cta_public_text if use_custom_emoji else self._cta_text
        if cta_url:
            lines.append("")
            link = html.escape(cta_url, quote=True)
            lines.append(f'👉 <a href="{link}">{html.escape(cta_text)}</a>')

        # Keep money and source intact. Drop secondary blocks before photo captions overflow.
        result = "\n".join(lines)
        if _telegram_length(result) > _CAPTION_LIMIT:
            lines = [ln for ln in lines if not ln.startswith(("#", "👉", "🛋"))]
            result = "\n".join(lines).strip()
        return result

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
        rental_terms_unavailable = False
        if (not self._dry_run and self._bot is not None and self._storage is not None
                and scored.listing.source == Source.AVITO):
            cached = await self._storage.get_listing(
                Source.AVITO.value, scored.listing.external_id,
            )
            if cached is not None:
                scored = scored.model_copy(update={"listing": cached})
        if (
            not self._dry_run
            and self._bot is not None
            and scored.listing.source == Source.AVITO
            and scored.listing.enriched_at is None
        ):
            detail = await fetch_detail(
                scored.listing.url, price=scored.listing.price,
                proxy=self._avito_proxy, limit=cap,
            )
            if detail is not None:
                if self._storage is not None:
                    await self._storage.save_enrichment(
                        Source.AVITO.value, scored.listing.external_id,
                        enriched_at=datetime.now(UTC).replace(tzinfo=None), **detail._asdict(),
                    )
                avito_full = detail.photos
                patch: dict = {}
                for field in (
                    "deposit_rub", "commission_pct", "commission_rub", "deposit_due_rub",
                    "deposit_installments",
                ):
                    patch[field] = getattr(detail, field)
                # Missing markup is not evidence that a known search-card
                # payment disappeared. Explicit 'уточняется' is still unknown.
                if not detail.commission_present:
                    patch.pop("commission_pct")
                    patch.pop("commission_rub")
                if not detail.deposit_present:
                    for field in ("deposit_rub", "deposit_due_rub", "deposit_installments"):
                        patch.pop(field)
                patch["upfront_payments"] = detail.upfront_payments or {}
                for field in (
                    "utilities_rub", "meters_included", "utilities_included",
                ):
                    value = getattr(detail, field)
                    if value is not None:
                        patch[field] = value
                if detail.commission_rub is not None:
                    patch["commission_pct"] = None
                elif detail.commission_pct is not None:
                    patch["commission_rub"] = None
                if patch:
                    scored = scored.model_copy(
                        update={"listing": scored.listing.model_copy(update=patch)}
                    )
            else:
                rental_terms_unavailable = True

        text = self.render(scored, use_custom_emoji=use_custom_emoji,
                           rental_terms_unavailable=rental_terms_unavailable)
        if self._dry_run or self._bot is None:
            logger.info("[DRY-RUN] → %s\n%s", channel, text)
            return True

        kb = listing_keyboard(scored.listing.url, personal=use_custom_emoji)
        photos: list = avito_full or _select_photos(scored.listing, cap)
        # Telegram не может стянуть фото Авито с CDN — скачиваем сами (наш IP
        # работает) и грузим байтами. Полноразмер уже получен выше.
        if photos and scored.listing.source == Source.AVITO:
            photos = await self._download_photos(avito_full or photos)
            sizes = [image_dimensions(photo.data) for photo in photos]
            if sizes and all(size is not None and max(size) < 640 for size in sizes):
                # A blocked original must not be presented as a sharp full-size image.
                # The shorter link label keeps caption length and source intact.
                text = text.replace("Открыть объявление на источнике", "Полные фото — в объявлении")
        elif photos and scored.listing.source == Source.CIAN:
            # Cian's CDN rejects Telegram downloads, and .jpg can contain WebP.
            # Fetch from our host and upload validated JPEG/PNG bytes instead.
            photos = await self._download_photos(photos, source=Source.CIAN)
        # Защита от кражи контента: в личке (платное) — по protect_bot, в канале —
        # по protect_public (use_custom_emoji=True только в личных чатах).
        protect = self._protect_bot if use_custom_emoji else self._protect_public
        # Уровни деградации: полный альбом → только первое фото → текст без фото.
        for level in (photos, photos[:1], []):
            if await self._try_send(channel, text, level, kb, protect=protect):
                return True
        logger.error("Не удалось отправить пост в %s (все фолбэки исчерпаны)", channel)
        return False

    async def _download_photos(
        self, urls: list[str], *, source: Source = Source.AVITO,
    ) -> list[BufferedInputFile]:
        """Скачать фото и завернуть в байты для загрузки (обход блокировки CDN)."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            )
        }
        out: list[BufferedInputFile] = []
        async with httpx.AsyncClient(
            timeout=20, headers=headers, follow_redirects=True,
            proxy=self._avito_proxy if source == Source.AVITO else None,
        ) as cl:
            for i, url in enumerate(urls):
                try:
                    resp = await cl.get(url)
                    resp.raise_for_status()
                    content, extension = await asyncio.to_thread(telegram_photo, resp.content)
                    out.append(BufferedInputFile(content, filename=f"photo{i}.{extension}"))
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    logger.warning("не скачал фото %s: %s", url, exc)
        return out

    async def _try_send(
        self,
        channel: str,
        text: str,
        photos: list,
        kb: InlineKeyboardMarkup,
        *,
        protect: bool = False,
    ) -> bool:
        """Одна попытка отправки на заданном уровне фолбэка (с учётом флуда).
        `protect` — запрет пересылки/сохранения (Telegram protect_content)."""
        assert self._bot is not None
        for _ in range(2):  # повтор после ожидания флуд-лимита
            try:
                if len(photos) >= 2 and _telegram_length(text) <= _CAPTION_LIMIT:
                    media = [
                        InputMediaPhoto(media=url, caption=text, parse_mode=ParseMode.HTML)
                        if i == 0
                        else InputMediaPhoto(media=url)
                        for i, url in enumerate(photos)
                    ]
                    messages = await self._bot.send_media_group(
                        chat_id=channel, media=media, protect_content=protect
                    )
                    if messages:
                        await self._attach_keyboard(channel, messages[-1].message_id, kb)
                elif photos and _telegram_length(text) <= _CAPTION_LIMIT:
                    await self._bot.send_photo(
                        chat_id=channel, photo=photos[0], caption=text,
                        reply_markup=kb, protect_content=protect,
                    )
                else:
                    await self._bot.send_message(
                        chat_id=channel, text=text, reply_markup=kb,
                        disable_web_page_preview=False, protect_content=protect,
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

    async def _attach_keyboard(
        self, channel: str, message_id: int, keyboard: InlineKeyboardMarkup
    ) -> None:
        # The album is already delivered: retry only this edit, never the media.
        assert self._bot is not None
        for _ in range(2):
            try:
                await self._bot.edit_message_reply_markup(
                    chat_id=channel, message_id=message_id, reply_markup=keyboard
                )
                return
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramAPIError:
                logger.warning("Не удалось прикрепить кнопки к доставленному альбому")
                return

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


def _telegram_length(value: str) -> int:
    # Telegram counts text after HTML parsing; UTF-16 units are conservative for emoji.
    plain = html.unescape(re.sub(r"<[^>]*>", "", value))
    return len(plain.encode("utf-16-le")) // 2


def _move_in_line(listing: Listing, *, unavailable: bool = False) -> str:
    """Разовые платежи при заезде. None — неизвестно, 0 — «без». Неполную сумму не
    выдаём за итог: «от X ₽» и что не указано. ЖКУ сюда не входят (они помесячно)."""
    deposit = listing.deposit_due
    commission = listing.commission_amount
    extras = {k: v for k, v in listing.upfront_payments.items() if v is not None}
    extra_text = "".join(f" + {html.escape(k)} {_money(v)} ₽" for k, v in extras.items())
    if listing.move_in_total is not None:
        dep = f"залог {_money(deposit)} ₽" if deposit else "без залога"
        com = f"комиссия {_money(commission)} ₽" if commission else "без комиссии"
        if commission and listing.commission_pct is not None and listing.commission_rub is None:
            com += f" ({listing.commission_pct}%)"
        if listing.deposit_installments:
            dep += (f" сейчас (всего {_money(listing.deposit_rub)} ₽)"
                    if listing.deposit_rub else " сейчас")
        return (f"🔑 <b>При заезде: {_money(listing.move_in_total)} ₽</b> "
                f"— первый месяц {_money(listing.price)} ₽ + {dep} + {com}{extra_text}")
    amounts, zeros, missing = ["первый месяц"], [], []
    if deposit:
        amounts.append(f"залог {_money(deposit)} ₽")
    elif deposit == 0:
        zeros.append("без залога")
    elif listing.deposit_installments and listing.deposit_rub is not None:
        amounts.append(f"залог всего {_money(listing.deposit_rub)} ₽ (платёж сейчас неизвестен)")
    else:
        missing.append("залог")
    if commission:
        amounts.append(f"комиссия {_money(commission)} ₽")
    elif commission == 0:
        zeros.append("без комиссии")
    else:
        missing.append("комиссия")
    missing.extend(k for k, v in listing.upfront_payments.items() if v is None)
    known = listing.price + (deposit or 0) + (commission or 0) + sum(extras.values())
    line = f"🔑 При заезде: от {_money(known)} ₽"
    if len(amounts) > 1 or extras:
        line += " — " + " + ".join(amounts) + extra_text
    tail = [*zeros, _missing_text(missing, unavailable=unavailable)] if missing else zeros
    return ", ".join([line, *tail])


def _missing_text(items: list[str], *, unavailable: bool) -> str:
    """«залог не указан», «залог и комиссия не указаны»; если площадка не отдала
    страницу условий — «… — в объявлении» (автор мог их и указать)."""
    names = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " и " + items[-1]
    names = html.escape(names)
    if unavailable:
        return f"{names} — в объявлении"
    if len(items) > 1:
        return f"{names} не указаны"
    return f"{names} " + {"залог": "не указан", "комиссия": "не указана"}.get(
        items[0], "не указано"
    )


def _utilities_suffix(listing: Listing) -> str:
    """Ежемесячные доплаты к аренде; неизвестное помечаем кратко, не додумываем."""
    utilities: str | None = None
    if listing.utilities_rub:
        utilities = f"+ {_money(listing.utilities_rub)} ₽ ЖКУ"
    elif listing.utilities_rub == 0 or listing.utilities_included is True:
        utilities = "ЖКУ включены"
    elif listing.utilities_included is False:
        utilities = "ЖКУ отдельно"
    meters: str | None = None
    if listing.meters_included is True:
        meters = "счётчики включены"
    elif listing.meters_included is False:
        meters = "счётчики отдельно"
    if utilities is None and meters is None:
        return "ЖКУ и счётчики не указаны"
    if (utilities, meters) == ("ЖКУ включены", "счётчики включены"):
        return "ЖКУ и счётчики включены"
    if (utilities, meters) == ("ЖКУ отдельно", "счётчики отдельно"):
        return "ЖКУ и счётчики отдельно"
    return ", ".join([utilities or "ЖКУ не указаны", meters or "счётчики не указаны"])


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
