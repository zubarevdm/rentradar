"""Персональный бот (aiogram): диалог настройки фильтра, команды, ручная оплата.

Один и тот же Telegram-бот постит топ-5 в публичный канал И обслуживает личные
чаты подписчиков. Здесь — роутер для личных чатов: создание фильтра через FSM,
просмотр/пауза/удаление, статус подписки, ручная покупка.
"""

from __future__ import annotations

import html
import logging
import secrets
from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
    PreCheckoutQuery,
)

from ..config import Settings
from .access import TARIFFS, moscow
from .filters import MAX_INTERVAL_MIN, MIN_INTERVAL_MIN, UserFilter
from .metro import STATIONS, neighbours, search_stations
from .navigation import main_menu
from .parsing import is_skip, parse_price_range, parse_rooms
from .store import PersonalStore

logger = logging.getLogger(__name__)

INTERVALS = [5, 15, 30, 60, 180, 1440]  # 5 мин = «Сразу», 1440 = раз в день
METRO_MINUTE_OPTIONS = (5, 10, 15, 20)

# Опции комнат: ключ кнопки → (подпись, какие значения rooms покрывает).
ROOM_OPTIONS: list[tuple[str, str, list[int]]] = [
    ("studio", "Студия", [0]),
    ("1", "1", [1]),
    ("2", "2", [2]),
    ("3plus", "3 и более", [3, 4, 5, 6]),
]

# Опции ремонта (одиночный выбор): ключ → (подпись, renovation_min). None = любой.
# «Только дизайнерский» убран: нейросеть ставит ярлык designer крайне редко (~0.2%
# рынка), фильтр давал пусто. Верхняя реальная планка — «современный/евро» (modern+),
# ниже — «без убитого» (simple+, отсекает soviet/needs_repair).
RENOVATION_OPTIONS: list[tuple[str, str, str | None]] = [
    ("any", "Любой", None),
    ("simple", "Приличный", "simple"),
    ("modern", "Современный", "modern"),
]

# Подписи кнопок главного меню (reply-клавиатура снизу).
BTN_NEW = "🔍 Новый поиск"
BTN_LIST = "📋 Мои фильтры"
BTN_STATUS = "ℹ️ Статус"
BTN_BUY = "💳 Подписка"
BTN_INVITE = "🎁 Пригласить"
BTN_SUPPORT = "🆘 Поддержка"

_METRO_INTRO = (
    "🚇 <b>Метро</b>\n"
    "Напиши станцию — покажу варианты с линиями 👇\n"
    "Можно выбрать несколько групп. Не нужно — жми «Продолжить без новых станций»."
)


def _kb_skip(action: str) -> InlineKeyboardMarkup:
    """Кнопка «Пропустить» для необязательных шагов (чтобы не писать слово руками)."""
    button = InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"skip:{action}")
    return _filter_keyboard(InlineKeyboardMarkup(inline_keyboard=[[button]]), action)


def _with_back(
    keyboard: InlineKeyboardMarkup | None = None,
    callback: str = "nav:menu",
    *,
    menu: bool = False,
) -> InlineKeyboardMarkup:
    rows = list(keyboard.inline_keyboard) if keyboard else []
    navigation = [InlineKeyboardButton(text="⬅️ Назад", callback_data=callback)]
    if menu:
        navigation.append(InlineKeyboardButton(text="☰ Меню", callback_data="nav:menu"))
    return InlineKeyboardMarkup(inline_keyboard=[*rows, navigation])


def _filter_keyboard(keyboard: InlineKeyboardMarkup, step: str) -> InlineKeyboardMarkup:
    return _with_back(keyboard, f"filter_back:{step}", menu=True)


class NewFilter(StatesGroup):
    rooms = State()
    price = State()
    metro = State()
    max_metro = State()
    renovation = State()
    no_commission = State()
    interval = State()
    backlog = State()


def build_router(store: PersonalStore, settings: Settings) -> Router:
    router = Router()
    router.startup.register(configure_menu)

    # ── общие действия (используются и командами, и кнопками меню) ──────
    async def _do_new(message: Message, state: FSMContext, user_id: int, bot: Bot) -> None:
        if message.from_user is None:
            return
        # Гейт: настроить поиск можно только с АКТИВНЫМ доступом (бесплатный период
        # за подписку/рефералов или платная подписка). Нет доступа → зовём получить.
        if not await store.is_active(user_id, _utcnow()):
            if not await _is_subscribed(bot, user_id):
                await _ask_subscribe(message, user_id)  # подписка на канал → бесплатный период
            else:
                await _ask_upgrade(message)  # период кончился → пригласить/оформить
            return
        existing = await store.list_filters(user_id)
        if len(existing) >= settings.max_filters_per_user:
            await message.answer(
                f"У тебя уже максимум фильтров ({settings.max_filters_per_user}). "
                "Удали ненужный в «📋 Мои фильтры», чтобы добавить новый.",
                reply_markup=_main_menu(),
            )
            return
        await state.clear()
        await state.update_data(city="Москва", rooms_sel=[])  # пока работаем только по Москве
        await state.set_state(NewFilter.rooms)
        await message.answer(
            "Сколько комнат? Выбери варианты (можно несколько) и нажми «Готово».\n"
            "Ничего не выбрано — подойдут любые.",
            reply_markup=_kb_rooms([]),
        )

    async def _ask_price(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await state.set_state(NewFilter.price)
        await message.answer(
            "Бюджет, ₽/мес. Примеры: <code>30000-70000</code>, <code>до 60000</code>, "
            "<code>от 40000</code>. Не важно — жми «Пропустить».",
            reply_markup=_kb_skip("price"),
        )

    async def _do_myfilters(message: Message, user_id: int | None = None) -> None:
        if message.from_user is None:
            return
        items = await store.list_filters(user_id or message.from_user.id)
        if not items:
            await message.answer(
                "У тебя пока нет фильтров.",
                reply_markup=_with_back(InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="➕ Добавить", callback_data="new_filter")]
                    ]
                )),
            )
            return
        for fid, flt in items:
            await message.answer(
                _describe(flt),
                reply_markup=_with_back(InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{fid}")]
                    ]
                )),
            )

    def _support_link() -> str:
        contact = settings.admin_contact
        url = "https://t.me/" + contact.lstrip("@") if contact.startswith("@") else contact
        if url.startswith(("https://", "http://", "tg://")):
            return f'<a href="{html.escape(url, quote=True)}">{html.escape(contact)}</a>'
        return html.escape(contact)

    async def _status_text(uid: int) -> str:
        now = _utcnow()
        access = await store.access(uid, now)
        parts = []
        if access.active(now) and access.until:
            parts.append(f"✅ Доступ действует до {moscow(access.until)}.")
        else:
            parts.append(
                "Сейчас активного доступа нет. /bonus — проверить право на пробные 24 часа."
            )
        if access.trial_until and access.trial_until > now:
            parts.append(
                f"Пробный доступ до {moscow(access.trial_until)}; пауза его не продлевает."
            )
        if access.paused:
            hours = access.frozen_seconds / 3600
            parts.append(
                f"⏸ Оплата/бонусы на паузе: {hours:g} ч. "
                f"Пауза до {settings.pause_max_days} дней, затем возобновится сама."
            )
        return "\n".join(parts)

    async def _do_status(message: Message, user_id: int | None = None) -> None:
        if message.from_user is None:
            return
        uid = user_id or message.from_user.id
        paused = await store.is_paused(uid, _utcnow())
        await message.answer(
            await _status_text(uid),
            reply_markup=_with_back(InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="▶️ Возобновить" if paused else "⏸ Пауза оплаты/бонусов",
                            callback_data="sub_resume" if paused else "sub_pause",
                        )
                    ]
                ]
            )),
        )

    async def _do_buy(message: Message, user_id: int | None = None) -> None:
        if message.from_user is None:
            return
        await message.answer(
            await _status_text(user_id or message.from_user.id) + "\n\n"
            "Для оплаты напиши администратору. После подтверждения оплаты мы подключим "
            "доступ вручную.\nЕсть вопросы по поиску, доступу или оплате? Напиши "
            + _support_link(),
            reply_markup=_with_back(InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"{t.title} · {t.price} ₽",
                            callback_data=f"tariff:{t.code}",
                        )
                    ]
                    for t in TARIFFS.values()
                ]
            )),
            disable_web_page_preview=True,
        )

    @router.callback_query(F.data.startswith("tariff:"))
    async def choose_tariff(cq: CallbackQuery) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        code = (cq.data or "").split(":", 1)[-1]
        plan = TARIFFS.get(code)
        if plan is None:
            await cq.answer("Неизвестный тариф", show_alert=True)
            return
        await cq.message.answer(
            f"{plan.title}, <b>{plan.price} ₽</b>.\n"
            f"Для оплаты напиши {_support_link()}. После подтверждения оплаты мы "
            "подключим доступ вручную.\nПередай администратору тариф "
            f"<code>{plan.code}</code> и свой Telegram ID: <code>{cq.from_user.id}</code>.",
            reply_markup=_with_back(callback="nav:buy"),
        )
        await cq.answer()

    # ── обязательная подписка на канал (гейт) ───────────────────────────
    async def _is_subscribed(bot: Bot, uid: int) -> bool:
        """Подписан ли пользователь на наш канал. Канал не настроен → гейт выкл."""
        chan = settings.public_channel
        if not chan:
            return True
        try:
            member = await bot.get_chat_member(chan, uid)
            return member.status in ("creator", "administrator", "member") or (
                member.status == "restricted" and bool(getattr(member, "is_member", False))
            )
        except Exception:  # noqa: BLE001 — бот не админ канала / приватный и т.п.
            return False

    async def _notify_activation(bot: Bot, ref_id: int | None) -> None:
        if ref_id is not None:
            try:
                await bot.send_message(
                    ref_id,
                    "🎁 Твой друг проверил подписку и настроил поиск. "
                    "Тебе начислено +12 часов доступа!",
                )
            except Exception:
                logger.warning("Не удалось отправить уведомление об активации друга")

    async def _verify_channel(bot: Bot, uid: int) -> bool:
        granted, ref_id = await store.verify_channel_with_reward(uid, _utcnow())
        await _notify_activation(bot, ref_id)
        return granted

    def _subscribe_kb() -> InlineKeyboardMarkup:
        chan = settings.public_channel
        url = f"https://t.me/{chan.lstrip('@')}" if chan.startswith("@") else chan
        rows = (
            [[InlineKeyboardButton(text="📢 Открыть канал", url=url)]]
            if url.startswith(("https://", "http://", "tg://"))
            else []
        )
        rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")])
        return _with_back(InlineKeyboardMarkup(inline_keyboard=rows))

    async def _ask_subscribe(message: Message, user_id: int | None = None) -> None:
        if message.from_user is None:
            return
        uid = user_id if user_id is not None else message.from_user.id
        row = await store.get_or_create_subscriber(uid)
        if row.trial_eligible and row.trial_until is None:
            text = (
                "Привет! Я FlatLikeThat 👋 Помогу искать квартиру: подберу объявления по твоим "
                "параметрам и пришлю новые подходящие варианты. Подпишись на наш канал и нажми "
                "«Проверить подписку» — после проверки получишь 24 часа пробного доступа. "
                "Затем настроим метро, бюджет и другие пожелания."
            )
        else:
            text = (
                await _status_text(uid) + "\nПробник повторно не выдаётся. "
                "Проверить подписку на канал можно кнопкой ниже. /buy — тарифы."
            )
        await message.answer(text, reply_markup=_subscribe_kb(), disable_web_page_preview=True)

    async def _ask_upgrade(message: Message) -> None:
        if message.from_user is None:
            return
        await message.answer(
            "Для поиска нужен активный доступ. /bonus — проверить право на пробные 24 часа.\n"
            "🎁 Пригласи друга: 12 часов за активного друга и ещё 2 дня за его первую оплату.\n"
            f"💳 Подписка — выбери тариф от {min(t.price for t in TARIFFS.values())} ₽.",
            reply_markup=_main_menu(),
        )

    async def _welcome_ready(message: Message, uid: int, granted: bool) -> None:
        if message.from_user is None:
            return
        access = await store.access(uid, _utcnow())
        if granted and access.trial_until:
            text = (
                f"Готово! Пробный доступ действует до {moscow(access.trial_until)}. "
                "Давай настроим поиск"
            )
        else:
            text = await _status_text(uid)
        await message.answer(text + "\nНажми «🔍 Новый поиск» внизу.", reply_markup=_main_menu())

    # ── онбординг ───────────────────────────────────────────────────────
    @router.message(CommandStart())
    async def start(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        uid = message.from_user.id
        parts = (message.text or "").split(maxsplit=1)
        ref = None
        if len(parts) == 2 and parts[1].startswith("ref_") and parts[1][4:].isdecimal():
            ref = int(parts[1][4:])
        await store.get_or_create_subscriber(uid, message.from_user.username, referred_by=ref)
        if await store.is_active(uid, _utcnow()):
            await _welcome_ready(message, uid, False)
        elif settings.public_channel:
            await _ask_subscribe(message)
        else:
            granted = await _verify_channel(bot, uid)
            await _welcome_ready(message, uid, granted)

    @router.callback_query(F.data == "check_sub")
    async def cb_check_sub(cq: CallbackQuery, bot: Bot) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        uid = cq.from_user.id
        if not await _is_subscribed(bot, uid):
            await cq.answer(
                "Пока не вижу твою подписку. Подпишись и нажми ещё раз.", show_alert=True
            )
            return
        granted = await _verify_channel(bot, uid)
        await _welcome_ready(cq.message, uid, granted)
        await cq.answer()

    @router.message(Command("menu"))
    async def show_menu(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Выбери действие 👇", reply_markup=_main_menu())

    @router.callback_query(F.data.startswith("nav:"))
    async def navigate(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        uid = cq.from_user.id
        action = cq.data.split(":", 1)[1]
        await cq.answer()
        if action == "new":
            await _do_new(cq.message, state, uid, bot)
        elif action == "filters":
            await _do_myfilters(cq.message, uid)
        elif action == "status":
            await _do_status(cq.message, uid)
        elif action == "buy":
            await _do_buy(cq.message, uid)
        elif action == "invite":
            await _do_invite(cq.message, bot, uid)
        elif action == "support":
            await _do_support(cq.message)
        else:
            await state.clear()
            await cq.message.answer("Выбери действие 👇", reply_markup=_main_menu())

    # ── кнопки меню (регистрируем ДО FSM, чтобы работали и в диалоге) ────
    @router.message(F.text == BTN_NEW)
    async def btn_new(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            return
        await _do_new(message, state, message.from_user.id, bot)

    @router.message(F.text == BTN_LIST)
    async def btn_list(message: Message) -> None:
        if message.from_user is None:
            return
        await _do_myfilters(message)

    @router.message(F.text == BTN_STATUS)
    async def btn_status(message: Message) -> None:
        if message.from_user is None:
            return
        await _do_status(message)

    @router.message(F.text == BTN_BUY)
    async def btn_buy(message: Message) -> None:
        if message.from_user is None:
            return
        await _do_buy(message)

    async def _do_invite(message: Message, bot: Bot, user_id: int | None = None) -> None:
        if message.from_user is None:
            return
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{user_id or message.from_user.id}"
        await message.answer(
            "🎁 12 часов за активного друга и ещё 2 дня за его первую оплату.\n"
            "Активный друг — новый пользователь по твоей ссылке, который проверил подписку "
            f"на канал и сохранил первый поиск.\nТвоя ссылка:\n{link}",
            reply_markup=_with_back(),
            disable_web_page_preview=True,
        )

    async def _do_support(message: Message) -> None:
        if message.from_user is None:
            return
        await message.answer(
            "Есть вопросы по поиску, доступу или оплате? Напиши " + _support_link(),
            reply_markup=_with_back(),
            disable_web_page_preview=True,
        )

    @router.message(Command("help"))
    async def help_user(message: Message) -> None:
        if message.from_user is None:
            return
        await message.answer(
            "/menu — главное меню; /new — новый поиск; "
            "/myfilters — твои фильтры; /status — доступ; "
            "/bonus — проверка пробника; /buy — тарифы; /invite — друзья; "
            "/cancel — отмена настройки.\nПробник: 24 часа после проверки канала."
        )
        await _do_support(message)

    @router.message(F.text == BTN_INVITE)
    async def btn_invite(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        await _do_invite(message, bot)

    @router.message(F.text == BTN_SUPPORT)
    async def btn_support(message: Message) -> None:
        if message.from_user is None:
            return
        await _do_support(message)

    # ── команды (дублируют кнопки) ──────────────────────────────────────
    @router.message(Command("new"))
    async def cmd_new(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None:
            return
        await _do_new(message, state, message.from_user.id, bot)

    @router.message(Command("myfilters"))
    async def cmd_list(message: Message) -> None:
        if message.from_user is None:
            return
        await _do_myfilters(message)

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        if message.from_user is None:
            return
        await _do_status(message)

    @router.message(Command("buy"))
    async def cmd_buy(message: Message) -> None:
        if message.from_user is None:
            return
        await _do_buy(message)

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await state.clear()
        await message.answer("Отменено.", reply_markup=_main_menu())

    @router.message(Command("myid"))
    async def my_id(message: Message) -> None:
        if message.from_user is None:
            return
        await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")

    @router.message(Command("invite"))
    async def cmd_invite(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        await _do_invite(message, bot)

    @router.message(Command("support"))
    async def cmd_support(message: Message) -> None:
        if message.from_user is None:
            return
        await _do_support(message)

    @router.message(Command("bonus"))
    async def cmd_bonus(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        uid = message.from_user.id
        if not await _is_subscribed(bot, uid):
            await _ask_subscribe(message)
            return
        granted = await _verify_channel(bot, uid)
        await _welcome_ready(message, uid, granted)

    @router.message(Command("emoji"))
    async def emoji_ids(message: Message) -> None:
        """Захват id кастом-эмодзи: /emoji и сразу нужные эмодзи в этом же сообщении.
        Открыта всем — возвращает только id (не секрет), вреда нет."""
        if message.from_user is None:
            return
        text = message.text or ""
        found = [
            (text[e.offset : e.offset + e.length], e.custom_emoji_id)
            for e in (message.entities or [])
            if e.type == "custom_emoji"
        ]
        if not found:
            await message.answer(
                "Пришли так: <code>/emoji</code> и сразу нужные кастом-эмодзи в этом "
                "же сообщении (нужен Telegram Premium, чтобы их вставить)."
            )
            return
        rows = "\n".join(f"{ch} → <code>{cid}</code>" for ch, cid in found)
        await message.answer("ID кастом-эмодзи (впиши в emoji.json):\n" + rows)

    @router.message(Command("grant"))
    async def grant(message: Message) -> None:
        """Ручная выдача подарка админом: /grant <user_id> [дней]."""
        if message.from_user is None:
            return
        if message.from_user.id not in settings.admins:
            return
        parts = (message.text or "").split()
        if (
            len(parts) not in (2, 3)
            or not parts[1].isdecimal()
            or (len(parts) == 3 and (not parts[2].isdecimal() or int(parts[2]) <= 0))
        ):
            await message.answer(
                "Использование: /grant &lt;telegram_id&gt; [положительное число дней]"
            )
            return
        days = int(parts[2]) if len(parts) == 3 else 30
        try:
            await store.grant_hours(int(parts[1]), _utcnow(), days * 24)
        except (ValueError, OverflowError) as exc:
            await message.answer(html.escape(str(exc)))
            return
        await message.answer(f"✅ Подарено {days * 24} ч. " + await _status_text(int(parts[1])))

    @router.message(Command("paid"))
    async def manual_paid(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        if message.from_user.id not in settings.admins:
            return
        parts = (message.text or "").split()
        if len(parts) != 4 or not parts[1].isdecimal():
            await message.answer(
                "Использование: /paid &lt;telegram_id&gt; &lt;24h|3d|7d|30d&gt; "
                "&lt;operation_id&gt;"
            )
            return
        try:
            result = await store.confirm_payment(
                int(parts[1]), parts[2], parts[3], message.from_user.id, settings.admins, _utcnow()
            )
        except (ValueError, OverflowError) as exc:
            await message.answer(html.escape(str(exc)))
            return
        # The commit has completed. Notification failures never roll back/retry a credit.
        await message.answer(
            f"✅ Операция {html.escape(result.operation_id)} сохранена: "
            f"{TARIFFS[result.tariff].duration}, {result.price_rub} ₽. "
            f"Срок на момент операции: {moscow(result.result_until)}.\n"
            + await _status_text(result.user_id)
        )
        try:
            await bot.send_message(
                result.user_id,
                "✅ Администратор подтвердил оплату.\n" + await _status_text(result.user_id),
            )
            if result.referrer_id is not None:
                await bot.send_message(
                    result.referrer_id, "🎁 За первую оплату друга сохранена награда +48 часов."
                )
        except Exception:
            logger.warning("Не удалось отправить уведомление об оплате")

    # ── создание фильтра (FSM) ──────────────────────────────────────────
    @router.callback_query(NewFilter.rooms, F.data.startswith("r_tg:"))
    async def rooms_toggle(cq: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        key = cq.data.split(":", 1)[1]
        data = await state.get_data()
        sel = list(data.get("rooms_sel", []))
        if key in sel:
            sel.remove(key)
        else:
            sel.append(key)
        await state.update_data(rooms_sel=sel)
        await cq.message.edit_reply_markup(reply_markup=_kb_rooms(sel))
        await cq.answer()

    @router.callback_query(NewFilter.rooms, F.data == "r_done")
    async def rooms_done(cq: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        data = await state.get_data()
        rooms = _rooms_from_keys(data.get("rooms_sel", []))
        await state.update_data(rooms=rooms)
        await cq.message.edit_text("Комнат: " + _rooms_label(rooms))
        await _ask_price(cq.message, state)
        await cq.answer()

    @router.message(NewFilter.rooms)
    async def set_rooms(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        # Запасной путь: если ввели текстом («1 2 студия»), тоже принимаем.
        rooms = parse_rooms(message.text or "")
        await state.update_data(
            rooms=rooms,
            rooms_sel=[key for key, _, values in ROOM_OPTIONS if set(values) & set(rooms)],
        )
        await _ask_price(message, state)

    @router.callback_query(NewFilter.price, F.data == "skip:price")
    async def skip_price(cq: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        await state.update_data(price_min=None, price_max=None)
        await cq.message.edit_text("Бюджет: любой")
        await _ask_metro_search(cq.message, state)
        await cq.answer()

    @router.message(NewFilter.price)
    async def set_price(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        lo, hi = parse_price_range(message.text or "")
        await state.update_data(price_min=lo, price_max=hi)
        await _ask_metro_search(message, state)

    @router.callback_query(NewFilter.max_metro, F.data == "skip:maxmetro")
    async def skip_max_metro(cq: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        await state.update_data(max_metro_min=None)
        await cq.message.edit_text("Время пешком до метро: без ограничения")
        await _ask_renovation(cq.message, state)
        await cq.answer()

    async def _metro_view(
        message: Message,
        state: FSMContext,
        phase: str,
        ids: list[str],
        selected: list[str],
        *,
        edit: bool = False,
    ) -> None:
        version = secrets.token_hex(4)
        await state.update_data(metro_version=version, metro_phase=phase, metro_ids=ids)
        rows = []
        for sid in ids:
            station = STATIONS[sid]
            if phase == "search":
                label = f"{station.name} · {station.line} {station.line_name}"
            else:
                label = ("✅ " if station.key in selected else "▫️ ") + station.key
            rows.append([InlineKeyboardButton(text=label, callback_data=f"m:{version}:{sid}")])
        if phase == "group":
            rows.append([InlineKeyboardButton(text="Готово", callback_data=f"m:{version}:done")])
        elif phase == "summary":
            rows.extend(
                [
                    [
                        InlineKeyboardButton(
                            text="Добавить ещё станции", callback_data=f"m:{version}:more"
                        )
                    ],
                    [InlineKeyboardButton(text="Продолжить", callback_data=f"m:{version}:next")],
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Продолжить без новых станций", callback_data=f"m:{version}:next"
                    )
                ]
            )
        text = _METRO_INTRO if phase == "search" else _metro_prompt(selected)
        if phase == "group":
            root = (await state.get_data())["metro_root"]
            station = STATIONS[root]
            text = (
                f"🚇 {station.line_name}: {station.name}\n"
                "Выбери станции, затем нажми «Готово».\n"
                + "\n".join(f"{s.name}: {direction}" for s, direction in neighbours(root))
            )
        kb = _filter_keyboard(InlineKeyboardMarkup(inline_keyboard=rows), "metro")
        if edit:
            await message.edit_text(text, reply_markup=kb)
        else:
            await message.answer(text, reply_markup=kb)

    async def _ask_metro_search(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await state.set_state(NewFilter.metro)
        data = await state.get_data()
        await _metro_view(message, state, "search", [], data.get("metros_sel", []))

    @router.message(NewFilter.metro)
    async def metro_search(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        data = await state.get_data()
        selected = data.get("metros_sel", [])
        if is_skip(message.text or ""):
            await state.update_data(metros=selected)
            await _ask_max_metro(message, state)
            return
        found = search_stations(message.text or "")
        if not found:
            await message.answer(
                "Не нашёл станцию. Попробуй другое название.",
                reply_markup=_filter_keyboard(InlineKeyboardMarkup(inline_keyboard=[]), "metro"),
            )
            return
        await _metro_view(message, state, "search", [s.id for s in found], selected)

    @router.callback_query(NewFilter.metro, F.data.startswith("m:"))
    async def metro_action(cq: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        data = await state.get_data()
        parts = (cq.data or "").split(":")
        if len(parts) != 3 or parts[1] != data.get("metro_version"):
            await cq.answer("Эта клавиатура устарела. Используй последнее сообщение.")
            return
        action = parts[2]
        phase = str(data.get("metro_phase", ""))
        selected = list(data.get("metros_sel", []))
        ids = data.get("metro_ids", [])
        if action == "next" and phase in ("summary", "search"):
            await state.update_data(metros=selected, metro_version=None)
            await cq.message.edit_text(
                "Метро: " + (", ".join(selected) if selected else "без ограничения по станциям")
            )
            await _ask_max_metro(cq.message, state)
        elif action == "more" and phase == "summary":
            await _ask_metro_search(cq.message, state)
        elif action == "done" and phase == "group":
            names = {STATIONS[sid].key for sid in ids}
            selected = list(
                dict.fromkeys([s for s in selected if s not in names] + data.get("metro_draft", []))
            )
            await state.update_data(metros_sel=selected)
            await _metro_view(
                cq.message, state, "summary", _metro_ids(selected), selected, edit=True
            )
        elif action in ids and action in STATIONS:
            station = STATIONS[action]
            if phase == "search":
                group = [s.id for s, _ in neighbours(action)]
                draft = list(
                    dict.fromkeys(
                        [STATIONS[sid].key for sid in group if STATIONS[sid].key in selected]
                        + [station.key]
                    )
                )
                await state.update_data(metro_root=action, metro_draft=draft)
                await _metro_view(cq.message, state, "group", group, draft, edit=True)
            else:
                target = list(data.get("metro_draft", [])) if phase == "group" else selected
                if station.key in target:
                    target.remove(station.key)
                else:
                    target.append(station.key)
                key = "metro_draft" if phase == "group" else "metros_sel"
                await state.update_data({key: target})
                await _metro_view(cq.message, state, phase, ids, target, edit=True)
        else:
            await cq.answer("Используй последнее сообщение выбора метро.")
            return
        await cq.answer()

    @router.callback_query(F.data.startswith(("m:", "m_add:", "m_done", "skip:metro")))
    async def stale_metro(cq: CallbackQuery) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        await cq.answer("Выбор метро уже завершён или отменён. Начни новый поиск.")

    async def _ask_max_metro(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await state.set_state(NewFilter.max_metro)
        await message.answer(
            "Сколько минут пешком до метро подходит? Выбери максимальное время.\n"
            "Если близость метро не важна, нажми «Пропустить».",
            reply_markup=_kb_max_metro(),
        )

    @router.callback_query(NewFilter.max_metro, F.data.startswith("maxmetro:"))
    async def choose_max_metro(cq: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        value = cq.data.split(":", 1)[1]
        if value not in {str(minutes) for minutes in METRO_MINUTE_OPTIONS}:
            await cq.answer("Выбери время на кнопках под вопросом.")
            return
        minutes = int(value)
        await state.update_data(max_metro_min=minutes)
        await cq.message.edit_text(f"До метро: до {minutes} минут пешком")
        await _ask_renovation(cq.message, state)
        await cq.answer()

    @router.message(NewFilter.max_metro)
    async def set_max_metro(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await message.answer(
            "Выбери максимальное время пешком на кнопках. "
            "Если близость метро не важна, нажми «Пропустить».",
            reply_markup=_kb_max_metro(),
        )

    @router.callback_query(F.data.startswith(("maxmetro:", "skip:maxmetro")))
    async def stale_max_metro(cq: CallbackQuery) -> None:
        await cq.answer("Выбор времени уже завершён или отменён. Начни новый поиск.")

    async def _ask_renovation(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await state.set_state(NewFilter.renovation)
        await message.answer(
            "Какой ремонт ищем?\n"
            "Оценивает нейросеть по фото — «современный» отсекает убитые и "
            "«бабушкины» варианты.",
            reply_markup=_kb_renovation(),
        )

    @router.callback_query(NewFilter.renovation, F.data.startswith("rv:"))
    async def set_renovation(cq: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        key = cq.data.split(":", 1)[1]
        rmin = next((val for k, _, val in RENOVATION_OPTIONS if k == key), None)
        label = next((lbl for k, lbl, _ in RENOVATION_OPTIONS if k == key), "Любой")
        await state.update_data(renovation_min=rmin)
        await cq.message.edit_text("Ремонт: " + label)
        await _ask_no_commission(cq.message, state)
        await cq.answer()

    async def _ask_no_commission(message: Message, state: FSMContext) -> None:
        await state.set_state(NewFilter.no_commission)
        await message.answer(
            "Только без посредников?\n"
            "Оставлю варианты с комиссией 0 и отсею риелторов по тексту объявления.",
            reply_markup=_filter_keyboard(InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Да, без комиссии", callback_data="nc:1"),
                        InlineKeyboardButton(text="Не важно", callback_data="nc:0"),
                    ]
                ]
            ), "no_commission"),
        )

    @router.callback_query(NewFilter.no_commission, F.data.startswith("nc:"))
    async def set_no_commission(cq: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        nc = cq.data.split(":", 1)[1] == "1"
        await state.update_data(no_commission=nc)
        await cq.message.edit_text("Без посредников: " + ("да" if nc else "не важно"))
        await _ask_interval(cq.message, state)
        await cq.answer()

    async def _ask_interval(message: Message, state: FSMContext) -> None:
        await state.set_state(NewFilter.interval)
        await message.answer(
            "Как часто присылать новые варианты?\n"
            "🔔 Сразу — проверять чаще, для активного поиска.\n"
            "Реже — если не хочешь частых сообщений. "
            "Варианты поступают после сбора и проверки.",
            reply_markup=_kb_intervals(),
        )

    @router.callback_query(NewFilter.interval, F.data.startswith("int:"))
    async def set_interval(cq: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        interval = int(cq.data.split(":", 1)[1])
        await state.update_data(interval_min=interval)
        await cq.message.edit_text("Присылаю: " + _interval_label(interval).lstrip("🔔 "))
        await _ask_backlog(cq.message, state)
        await cq.answer()

    async def _ask_backlog(message: Message, state: FSMContext) -> None:
        await state.set_state(NewFilter.backlog)
        await message.answer(
            "Показывать те, что уже на рынке, или только новые?\n"
            "📋 Все подходящие: и то, что есть сейчас, и новое.\n"
            "🔔 Только новые: пару свежих сейчас, дальше лишь то, что появится.",
            reply_markup=_filter_keyboard(InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="📋 Все подходящие", callback_data="bl:1"),
                        InlineKeyboardButton(text="🔔 Только новые", callback_data="bl:0"),
                    ]
                ]
            ), "backlog"),
        )

    @router.callback_query(F.data.startswith("filter_back:"))
    async def filter_back(cq: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        step = cq.data.split(":", 1)[1]
        steps = ("rooms", "price", "metro", "max_metro", "renovation",
                 "no_commission", "interval", "backlog")
        if step not in steps or await state.get_state() != getattr(NewFilter, step).state:
            await cq.answer("Этот шаг уже завершён. Используй последнее сообщение.")
            return
        data = await state.get_data()
        await cq.answer()
        await cq.message.edit_reply_markup(reply_markup=None)
        if step == "rooms":
            await state.clear()
            await cq.message.answer("Выбери действие 👇", reply_markup=_main_menu())
        elif step == "price":
            await state.set_state(NewFilter.rooms)
            await cq.message.answer(
                "Сколько комнат? Выбери варианты (можно несколько) и нажми «Готово».\n"
                "Ничего не выбрано — подойдут любые.",
                reply_markup=_kb_rooms(data.get("rooms_sel", [])),
            )
        elif step == "metro":
            if data.get("metro_phase") in ("group", "summary"):
                await _ask_metro_search(cq.message, state)
            else:
                await _ask_price(cq.message, state)
        elif step == "max_metro":
            await state.set_state(NewFilter.metro)
            selected = data.get("metros_sel", data.get("metros", []))
            await _metro_view(cq.message, state, "summary", _metro_ids(selected), selected)
        elif step == "renovation":
            await _ask_max_metro(cq.message, state)
        elif step == "no_commission":
            await _ask_renovation(cq.message, state)
        elif step == "interval":
            await _ask_no_commission(cq.message, state)
        elif step == "backlog":
            await _ask_interval(cq.message, state)

    @router.callback_query(NewFilter.backlog, F.data.startswith("bl:"))
    async def set_backlog(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        data = await state.get_data()
        include_backlog = cq.data.split(":", 1)[1] == "1"
        flt = UserFilter(
            user_id=cq.from_user.id,
            city=data.get("city", "Москва"),
            rooms=data.get("rooms", []),
            price_min=data.get("price_min"),
            price_max=data.get("price_max"),
            metros=data.get("metros", []),
            max_metro_min=data.get("max_metro_min"),
            renovation_min=data.get("renovation_min"),
            no_commission=data.get("no_commission", False),
            interval_min=data.get("interval_min", 30),
            include_backlog=include_backlog,
        )
        if not await store.is_active(cq.from_user.id, _utcnow()):
            await cq.answer("Доступ закончился. Открой «Подписка» или /bonus.", show_alert=True)
            return
        _, ref_id = await store.save_filter(flt, now=_utcnow())
        await state.clear()
        await _notify_activation(bot, ref_id)
        since = _utcnow() - timedelta(hours=settings.personal_lookback_hours)
        found = await store.count_new_matches(flt, since)
        if found and include_backlog:
            tail = (
                f"\n\n🔔 Уже нашёл за неделю подходящих: <b>{found}</b>. "
                "Свежие покажу при ближайшей рассылке, остальные пришлю постепенно "
                "по выбранному интервалу."
            )
        elif found:
            tail = (
                f"\n\n🔔 Покажу пару свежих из <b>{found}</b> на рынке, а дальше буду "
                "присылать новые после обновления базы."
            )
        else:
            tail = (
                "\n\n🔍 Пока подходящих нет. Буду проверять новые по выбранному интервалу. "
                "Хочешь быстрее? Попробуй расширить критерии: станции, цену, комнаты."
            )
        await cq.message.edit_text(
            "✅ Фильтр сохранён!\n\n" + _describe(flt) + tail, reply_markup=_main_menu()
        )
        await cq.answer("Готово")

    # ── кнопка «Добавить» / удаление фильтра / оплата ───────────────────
    @router.callback_query(F.data == "new_filter")
    async def cb_new_filter(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        await cq.answer()
        await _do_new(cq.message, state, cq.from_user.id, bot)

    @router.callback_query(F.data.startswith("del:"))
    async def delete_filter(cq: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        try:
            fid = int(cq.data.split(":", 1)[1])
        except ValueError:
            await cq.answer("Фильтр не найден.")
            return
        ok = await store.delete_filter(fid, cq.from_user.id)
        if ok:
            await state.clear()
            await cq.message.edit_text("Фильтр удалён", reply_markup=_main_menu())
        else:
            await cq.message.edit_text("Фильтр не найден.", reply_markup=_main_menu())
        await cq.answer()

    @router.pre_checkout_query()
    async def pre_checkout(pcq: PreCheckoutQuery) -> None:
        await pcq.answer(ok=False, error_message="Оплата доступна через администратора: /buy")

    @router.message(F.successful_payment)
    async def legacy_payment(message: Message) -> None:
        if message.from_user is None:
            return
        await message.answer(
            "Обратись в поддержку с данными платежа: "
            + _support_link()
            + ". Доступ подтверждает администратор."
        )

    @router.callback_query(F.data == "sub_pause")
    async def cb_pause(cq: CallbackQuery) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        ok = await store.pause(cq.from_user.id, _utcnow())
        await cq.message.edit_text(
            f"⏸ Подписка на паузе до {settings.pause_max_days} дней — потом возобновится "
            "сама. Включить раньше можно кнопкой ниже."
            if ok
            else "Пауза доступна только при активной подписке.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▶️ Разморозить", callback_data="sub_resume"),
                InlineKeyboardButton(text="☰ Меню", callback_data="nav:menu"),
            ]]) if ok else _main_menu(),
        )
        await cq.answer()

    @router.callback_query(F.data == "sub_resume")
    async def cb_resume(cq: CallbackQuery) -> None:
        if not isinstance(cq.message, Message) or cq.data is None:
            await cq.answer()
            return
        ok = await store.resume(cq.from_user.id, _utcnow())
        await cq.message.edit_text(
            "▶️ Подписка возобновлена — снова присылаю варианты."
            if ok else "Подписка не на паузе.",
            reply_markup=_main_menu(),
        )
        await cq.answer()

    return router


# ── helpers ────────────────────────────────────────────────────────────────
def _main_menu() -> InlineKeyboardMarkup:
    return main_menu()


def _kb_max_metro() -> InlineKeyboardMarkup:
    options = [
        InlineKeyboardButton(text=f"До {minutes} минут", callback_data=f"maxmetro:{minutes}")
        for minutes in METRO_MINUTE_OPTIONS
    ]
    return _filter_keyboard(InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data="skip:maxmetro")],
        options[:2],
        options[2:],
    ]), "max_metro")


def _metro_prompt(selected: list[str]) -> str:
    if selected:
        return (
            "✅ Выбрано: <b>" + ", ".join(selected) + "</b>\n\n"
            "Нажми на станцию, чтобы убрать её.\n"
            "Добавь ещё станции или нажми «Продолжить»."
        )
    return "Станции не выбраны. «Продолжить» — поиск без ограничения по метро."


def _metro_ids(names: list[str]) -> list[str]:
    return [s.id for name in names for s in STATIONS.values() if name in (s.key, s.name)]


def _kb_rooms(selected: list[str]) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text=("✅ " if key in selected else "") + label, callback_data=f"r_tg:{key}"
        )
        for key, label, _ in ROOM_OPTIONS
    ]
    done = [InlineKeyboardButton(text="Готово ✓", callback_data="r_done")]
    return _filter_keyboard(InlineKeyboardMarkup(inline_keyboard=[row, done]), "rooms")


def _rooms_from_keys(selected: list[str]) -> list[int]:
    """Ключи выбранных кнопок → отсортированный список значений rooms (без дублей)."""
    return sorted({r for key, _, vals in ROOM_OPTIONS if key in selected for r in vals})


def _rooms_label(rooms: list[int]) -> str:
    """Компактная подпись: [0,1,3,4,5,6] → «студия, 1, 3+». Пусто → «любые»."""
    if not rooms:
        return "любые"
    rs = set(rooms)
    parts: list[str] = []
    if 0 in rs:
        parts.append("студия")
    if 1 in rs:
        parts.append("1")
    if 2 in rs:
        parts.append("2")
    if rs & {3, 4, 5, 6}:
        parts.append("3+")
    return ", ".join(parts)


def _interval_label(m: int) -> str:
    if m <= MIN_INTERVAL_MIN:
        return "🔔 Сразу"
    if m >= 1440:
        return "Раз в день"
    return f"{m} мин" if m < 60 else f"{m // 60} ч"


def _kb_intervals() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=_interval_label(m), callback_data=f"int:{m}")
        for m in INTERVALS
        if MIN_INTERVAL_MIN <= m <= MAX_INTERVAL_MIN
    ]
    # По 3 в ряд, чтобы кнопки не сжимались.
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    return _filter_keyboard(InlineKeyboardMarkup(inline_keyboard=rows), "interval")


def _kb_renovation() -> InlineKeyboardMarkup:
    return _filter_keyboard(InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"rv:{key}")]
            for key, label, _ in RENOVATION_OPTIONS
        ]
    ), "renovation")


def _renovation_filter_label(renovation_min: str) -> str:
    """renovation_min → подпись для описания фильтра."""
    return {
        "simple": "приличный",
        "modern": "современный",
        "designer": "дизайнерский",  # legacy — старые фильтры
    }.get(renovation_min, renovation_min)


def _describe(flt: UserFilter) -> str:
    parts = [f"🔎 <b>{flt.name}</b> — {flt.city}"]
    if flt.rooms:
        parts.append("комнат: " + _rooms_label(flt.rooms))
    if flt.price_min or flt.price_max:
        lo = f"{flt.price_min:,}".replace(",", " ") if flt.price_min else "0"
        hi = f"{flt.price_max:,}".replace(",", " ") if flt.price_max else "∞"
        parts.append(f"цена: {lo}–{hi} ₽")
    if flt.metros:
        parts.append("метро/район: " + ", ".join(flt.metros))
    if flt.max_metro_min is not None:
        parts.append(f"до метро: до {flt.max_metro_min} минут пешком")
    if flt.renovation_min:
        parts.append("ремонт: " + _renovation_filter_label(flt.renovation_min))
    if flt.no_commission:
        parts.append("без посредников")
    parts.append("присылаю: " + _interval_label(flt.clamp_interval()).lstrip("🔔 ").lower())
    if not flt.include_backlog:
        parts.append("режим: только новые")
    return "\n".join(parts)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def configure_menu(bot: Bot) -> None:
    """Populate Telegram's native menu for desktop and mobile clients."""
    commands = [
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="new", description="Новый поиск"),
        BotCommand(command="myfilters", description="Мои фильтры"),
        BotCommand(command="status", description="Статус доступа"),
        BotCommand(command="buy", description="Подписка и тарифы"),
        BotCommand(command="invite", description="Пригласить друга"),
        BotCommand(command="bonus", description="Проверить подписку на канал"),
        BotCommand(command="support", description="Поддержка"),
        BotCommand(command="cancel", description="Отменить настройку"),
        BotCommand(command="help", description="Справка"),
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramAPIError:
        logger.warning("Не удалось обновить меню Telegram; команда /menu остаётся доступной")
