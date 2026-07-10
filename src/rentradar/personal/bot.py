"""Персональный бот (aiogram): диалог настройки фильтра, команды, оплата ЮKassa.

Один и тот же Telegram-бот постит топ-5 в публичный канал И обслуживает личные
чаты подписчиков. Здесь — роутер для личных чатов: создание фильтра через FSM,
просмотр/пауза/удаление, статус подписки, покупка через ЮKassa.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)

from ..config import Settings
from .filters import MAX_INTERVAL_MIN, MIN_INTERVAL_MIN, UserFilter
from .metro import suggest_metros
from .parsing import is_skip, parse_int_or_none, parse_price_range, parse_rooms
from .store import PersonalStore

logger = logging.getLogger(__name__)

INTERVALS = [5, 15, 30, 60, 180]

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
    ("simple", "Без убитого (косметика и лучше)", "simple"),
    ("modern", "Хороший (современный/евро)", "modern"),
]

# Подписи кнопок главного меню (reply-клавиатура снизу).
BTN_NEW = "🔍 Новый поиск"
BTN_LIST = "📋 Мои фильтры"
BTN_STATUS = "ℹ️ Статус"
BTN_BUY = "💳 Подписка"

_METRO_INTRO = (
    "🚇 <b>Метро</b>\n"
    "Напишите станцию — покажу кнопками 👇\n"
    "Можно несколько. Не нужно — «пропустить»."
)


class NewFilter(StatesGroup):
    rooms = State()
    price = State()
    metro = State()
    max_metro = State()
    renovation = State()
    no_commission = State()
    interval = State()


def build_router(store: PersonalStore, settings: Settings) -> Router:
    router = Router()

    # ── общие действия (используются и командами, и кнопками меню) ──────
    async def _do_new(message: Message, state: FSMContext, user_id: int) -> None:
        existing = await store.list_filters(user_id)
        if len(existing) >= settings.max_filters_per_user:
            await message.answer(
                f"У вас уже максимум фильтров ({settings.max_filters_per_user}). "
                "Удалите ненужный в «📋 Мои фильтры», чтобы добавить новый."
            )
            return
        await state.clear()
        await state.update_data(city="Москва", rooms_sel=[])  # пока работаем только по Москве
        await state.set_state(NewFilter.rooms)
        await message.answer(
            "Сколько комнат? Выберите варианты (можно несколько) и нажмите «Готово».\n"
            "Ничего не выбрали — подойдут любые.",
            reply_markup=_kb_rooms([]),
        )

    async def _ask_price(message: Message, state: FSMContext) -> None:
        await state.set_state(NewFilter.price)
        await message.answer(
            "Бюджет, ₽/мес. Примеры: <code>30000-70000</code>, <code>до 60000</code>, "
            "<code>от 40000</code> или «пропустить»."
        )

    async def _do_myfilters(message: Message) -> None:
        items = await store.list_filters(message.from_user.id)
        if not items:
            await message.answer(
                "У вас пока нет фильтров.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="➕ Добавить", callback_data="new_filter")]
                    ]
                ),
            )
            return
        for fid, flt in items:
            await message.answer(
                _describe(flt),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{fid}")]
                    ]
                ),
            )

    async def _do_status(message: Message) -> None:
        uid = message.from_user.id
        now = _utcnow()
        if await store.is_paused(uid, now):
            left = await store.frozen_days_left(uid)
            await message.answer(
                f"⏸ Подписка на паузе (до {settings.pause_max_days} дн., потом "
                f"возобновится сама). Заморожено дней: <b>{left}</b>.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="▶️ Возобновить", callback_data="sub_resume")]
                    ]
                ),
            )
            return
        if await store.is_active(uid, now):
            until = await store.paid_until_of(uid)
            until_fmt = f" до {until:%d.%m.%Y}" if until else ""
            await message.answer(
                f"✅ Подписка активна{until_fmt}.\n"
                f"Нужен перерыв? Пауза до {settings.pause_max_days} дней — дни сохранятся.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="⏸ Пауза", callback_data="sub_pause")]
                    ]
                ),
            )
            return
        used = await store.free_sends_used(uid)
        left = max(0, settings.free_sends_limit - used)
        await message.answer(
            f"Подписки нет. Бесплатных отправок осталось: <b>{left}</b>.\n"
            f"🎁 Подпишись на {settings.public_channel} и получи <b>{settings.channel_bonus_days} "
            "дней</b> бесплатно — команда /bonus.\n"
            "Оформить подписку — кнопка «💳 Подписка».",
            disable_web_page_preview=True,
        )

    async def _do_buy(message: Message) -> None:
        if not settings.yookassa_provider_token:
            await message.answer(
                "Оплата картой скоро будет подключена. Напишите администратору для "
                "ручной активации."
            )
            return
        await message.answer_invoice(
            title="Подписка RentRadar",
            description=f"Доступ к персональному поиску на {settings.sub_days} дней",
            payload=f"sub:{settings.sub_days}",
            provider_token=settings.yookassa_provider_token,
            currency="RUB",
            prices=[LabeledPrice(label="Подписка", amount=settings.sub_price_rub * 100)],
        )

    # ── онбординг ───────────────────────────────────────────────────────
    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await store.get_or_create_subscriber(
            message.from_user.id, message.from_user.username
        )
        # Реферальный диплинк: /start ref_<id>.
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2 and parts[1].startswith("ref_") and parts[1][4:].isdigit():
            await store.set_referred_by(message.from_user.id, int(parts[1][4:]))
        await message.answer(
            "👋 Это персональный поиск квартир по вашим критериям.\n\n"
            "Настройте фильтр (метро, цена, комнаты, время до метро), задайте интервал — "
            "и я буду присылать новые подходящие варианты со всех площадок в этот чат.\n\n"
            f"Первые <b>{settings.free_sends_limit}</b> вариантов — бесплатно.\n"
            "Хочешь дни в подарок? Зови друзей — /invite.\n\n"
            "Пользуйтесь кнопками меню снизу 👇",
            reply_markup=_main_menu(),
        )

    # ── кнопки меню (регистрируем ДО FSM, чтобы работали и в диалоге) ────
    @router.message(F.text == BTN_NEW)
    async def btn_new(message: Message, state: FSMContext) -> None:
        await _do_new(message, state, message.from_user.id)

    @router.message(F.text == BTN_LIST)
    async def btn_list(message: Message) -> None:
        await _do_myfilters(message)

    @router.message(F.text == BTN_STATUS)
    async def btn_status(message: Message) -> None:
        await _do_status(message)

    @router.message(F.text == BTN_BUY)
    async def btn_buy(message: Message) -> None:
        await _do_buy(message)

    # ── команды (дублируют кнопки) ──────────────────────────────────────
    @router.message(Command("new"))
    async def cmd_new(message: Message, state: FSMContext) -> None:
        await _do_new(message, state, message.from_user.id)

    @router.message(Command("myfilters"))
    async def cmd_list(message: Message) -> None:
        await _do_myfilters(message)

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        await _do_status(message)

    @router.message(Command("buy"))
    async def cmd_buy(message: Message) -> None:
        await _do_buy(message)

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменено.", reply_markup=_main_menu())

    @router.message(Command("myid"))
    async def my_id(message: Message) -> None:
        await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")

    @router.message(Command("invite"))
    async def cmd_invite(message: Message, bot: Bot) -> None:
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
        await message.answer(
            "🎁 <b>Приглашай друзей</b>\n"
            f"Друг оформит подписку по твоей ссылке — тебе <b>+{settings.referral_days} дней</b>."
            f"\n\nТвоя ссылка:\n{link}",
            disable_web_page_preview=True,
        )

    @router.message(Command("bonus"))
    async def cmd_bonus(message: Message, bot: Bot) -> None:
        """Разовый бонус за подписку на публичный канал."""
        uid = message.from_user.id
        chan = settings.public_channel
        if not chan:
            await message.answer("Бонус-канал пока не настроен.")
            return
        try:
            member = await bot.get_chat_member(chan, uid)
            subscribed = member.status in ("creator", "administrator", "member", "restricted")
        except Exception:  # noqa: BLE001 — бот не админ канала / приватный и т.п.
            subscribed = False
        if not subscribed:
            await message.answer(
                f"Сначала подпишись на канал {chan}, потом снова нажми /bonus.",
                disable_web_page_preview=True,
            )
            return
        if await store.claim_channel_bonus(uid, _utcnow(), settings.channel_bonus_days):
            await message.answer(
                f"🎁 Готово! Начислено <b>{settings.channel_bonus_days} дней</b> подписки "
                "за подписку на канал. Настрой поиск — «🔍 Новый поиск»."
            )
        else:
            await message.answer("Бонус за канал ты уже получал 🙂")

    @router.message(Command("emoji"))
    async def emoji_ids(message: Message) -> None:
        """Захват id кастом-эмодзи: /emoji и сразу нужные эмодзи в этом же сообщении.
        Открыта всем — возвращает только id (не секрет), вреда нет."""
        text = message.text or ""
        found = [
            (text[e.offset : e.offset + e.length], e.custom_emoji_id)
            for e in (message.entities or [])
            if e.type == "custom_emoji"
        ]
        if not found:
            await message.answer(
                "Пришлите так: <code>/emoji</code> и сразу нужные кастом-эмодзи в этом "
                "же сообщении (нужен Telegram Premium, чтобы их вставить)."
            )
            return
        rows = "\n".join(f"{ch} → <code>{cid}</code>" for ch, cid in found)
        await message.answer("ID кастом-эмодзи (впишите в emoji.json):\n" + rows)

    @router.message(Command("grant"))
    async def grant(message: Message) -> None:
        """Ручная выдача подписки админом: /grant <user_id> [дней]."""
        if message.from_user.id not in settings.admins:
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("Использование: /grant &lt;user_id&gt; [дней]")
            return
        user_id = int(parts[1])
        days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else settings.sub_days
        until = _utcnow() + timedelta(days=days)
        await store.set_paid_until(user_id, until)
        await message.answer(f"✅ Пользователю {user_id} выдана подписка до {until:%d.%m.%Y}.")

    # ── создание фильтра (FSM) ──────────────────────────────────────────
    @router.callback_query(NewFilter.rooms, F.data.startswith("r_tg:"))
    async def rooms_toggle(cq: CallbackQuery, state: FSMContext) -> None:
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
        data = await state.get_data()
        rooms = _rooms_from_keys(data.get("rooms_sel", []))
        await state.update_data(rooms=rooms)
        await cq.message.edit_text("Комнат: " + _rooms_label(rooms))
        await _ask_price(cq.message, state)
        await cq.answer()

    @router.message(NewFilter.rooms)
    async def set_rooms(message: Message, state: FSMContext) -> None:
        # Запасной путь: если ввели текстом («1 2 студия»), тоже принимаем.
        await state.update_data(rooms=parse_rooms(message.text or ""))
        await _ask_price(message, state)

    @router.message(NewFilter.price)
    async def set_price(message: Message, state: FSMContext) -> None:
        lo, hi = parse_price_range(message.text or "")
        await state.update_data(price_min=lo, price_max=hi, metros_sel=[], metros_last=[])
        await state.set_state(NewFilter.metro)
        await message.answer(_METRO_INTRO)

    # ── метро: поиск-подсказка (typeahead) ──────────────────────────────
    @router.message(NewFilter.metro)
    async def metro_search(message: Message, state: FSMContext) -> None:
        text = message.text or ""
        if is_skip(text):
            await state.update_data(metros=[])
            await _ask_max_metro(message, state)
            return
        data = await state.get_data()
        city = data.get("city", "Москва")
        found = suggest_metros(text, await store.distinct_metros(city))
        if not found:
            await message.answer("Не нашёл такую станцию. Попробуйте иначе или «пропустить».")
            return
        await state.update_data(metros_last=found)
        await message.answer(
            _metro_prompt(data.get("metros_sel", [])),
            reply_markup=_kb_metro(found, data.get("metros_sel", [])),
        )

    @router.callback_query(NewFilter.metro, F.data.startswith("m_add:"))
    async def metro_toggle(cq: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        last = data.get("metros_last", [])
        idx = int(cq.data.split(":", 1)[1])
        if not (0 <= idx < len(last)):
            await cq.answer()
            return
        station = last[idx]
        sel = list(data.get("metros_sel", []))
        if station in sel:
            sel.remove(station)
        else:
            sel.append(station)
        await state.update_data(metros_sel=sel)
        await cq.message.edit_text(
            _metro_prompt(sel), reply_markup=_kb_metro(last, sel)
        )
        await cq.answer()

    @router.callback_query(NewFilter.metro, F.data == "m_done")
    async def metro_done(cq: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        sel = data.get("metros_sel", [])
        await state.update_data(metros=sel)
        await cq.message.edit_text("Метро: " + (", ".join(sel) if sel else "любые"))
        await _ask_max_metro(cq.message, state)
        await cq.answer()

    async def _ask_max_metro(message: Message, state: FSMContext) -> None:
        await state.set_state(NewFilter.max_metro)
        await message.answer("Максимум минут пешком до метро (число) или «пропустить».")

    @router.message(NewFilter.max_metro)
    async def set_max_metro(message: Message, state: FSMContext) -> None:
        await state.update_data(max_metro_min=parse_int_or_none(message.text or ""))
        await _ask_renovation(message, state)

    async def _ask_renovation(message: Message, state: FSMContext) -> None:
        await state.set_state(NewFilter.renovation)
        await message.answer(
            "Какой ремонт ищем?\n"
            "Оценивает нейросеть по фото — «современный» отсекает убитые и "
            "«бабушкины» варианты.",
            reply_markup=_kb_renovation(),
        )

    @router.callback_query(NewFilter.renovation, F.data.startswith("rv:"))
    async def set_renovation(cq: CallbackQuery, state: FSMContext) -> None:
        key = cq.data.split(":", 1)[1]
        rmin = next((val for k, _, val in RENOVATION_OPTIONS if k == key), None)
        label = next((lbl for k, lbl, _ in RENOVATION_OPTIONS if k == key), "Любой")
        await state.update_data(renovation_min=rmin)
        await cq.message.edit_text("Ремонт: " + label)
        await state.set_state(NewFilter.no_commission)
        await cq.message.answer(
            "Только без посредников?\n"
            "Оставлю варианты с комиссией 0 и отсею риелторов по тексту объявления.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Да, без комиссии", callback_data="nc:1"),
                        InlineKeyboardButton(text="Не важно", callback_data="nc:0"),
                    ]
                ]
            ),
        )
        await cq.answer()

    @router.callback_query(NewFilter.no_commission, F.data.startswith("nc:"))
    async def set_no_commission(cq: CallbackQuery, state: FSMContext) -> None:
        nc = cq.data.split(":", 1)[1] == "1"
        await state.update_data(no_commission=nc)
        await cq.message.edit_text("Без посредников: " + ("да" if nc else "не важно"))
        await state.set_state(NewFilter.interval)
        await cq.message.answer(
            "Как часто проверять? Выберите интервал:", reply_markup=_kb_intervals()
        )
        await cq.answer()

    @router.callback_query(NewFilter.interval, F.data.startswith("int:"))
    async def set_interval(cq: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        interval = int(cq.data.split(":", 1)[1])
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
            interval_min=interval,
        )
        await store.upsert_filter(flt)
        await state.clear()
        since = _utcnow() - timedelta(hours=settings.personal_lookback_hours)
        found = await store.count_new_matches(flt, since)
        if found:
            tail = f"\n\n🔔 Уже нашёл подходящих: <b>{found}</b> — пришлю в ближайшую минуту."
        else:
            tail = (
                "\n\n🔍 Пока подходящих вариантов нет — пришлю, как только появятся. "
                "Если ждёте быстрее, попробуйте расширить критерии (станции, цену, комнаты)."
            )
        await cq.message.edit_text("✅ Фильтр сохранён!\n\n" + _describe(flt) + tail)
        await cq.answer("Готово")

    # ── кнопка «Добавить» / удаление фильтра / оплата ───────────────────
    @router.callback_query(F.data == "new_filter")
    async def cb_new_filter(cq: CallbackQuery, state: FSMContext) -> None:
        await cq.answer()
        await _do_new(cq.message, state, cq.from_user.id)

    @router.callback_query(F.data.startswith("del:"))
    async def delete_filter(cq: CallbackQuery) -> None:
        fid = int(cq.data.split(":", 1)[1])
        ok = await store.delete_filter(fid, cq.from_user.id)
        await cq.message.edit_text("🗑 Фильтр удалён." if ok else "Не найдено.")
        await cq.answer()

    @router.pre_checkout_query()
    async def pre_checkout(pcq: PreCheckoutQuery) -> None:
        await pcq.answer(ok=True)

    @router.message(F.successful_payment)
    async def paid(message: Message, bot: Bot) -> None:
        now = _utcnow()
        until = now + timedelta(days=settings.sub_days)
        await store.set_paid_until(message.from_user.id, until)
        await message.answer(
            f"✅ Оплата получена! Подписка активна до {until:%d.%m.%Y}. "
            "Буду присылать все подходящие варианты."
        )
        # Реферал: если этого юзера пригласили — начислить пригласившему дни.
        ref_id = await store.credit_referral(message.from_user.id, now, settings.referral_days)
        if ref_id is not None:
            try:
                await bot.send_message(
                    ref_id,
                    f"🎁 Твой друг оформил подписку — тебе начислено "
                    f"<b>+{settings.referral_days} дней</b>! Спасибо 🙌",
                )
            except Exception:  # noqa: BLE001
                logger.warning("не смог уведомить реферера %s", ref_id)

    @router.callback_query(F.data == "sub_pause")
    async def cb_pause(cq: CallbackQuery) -> None:
        ok = await store.pause(cq.from_user.id, _utcnow())
        await cq.message.edit_text(
            f"⏸ Подписка на паузе до {settings.pause_max_days} дней — потом возобновится "
            "сама. Или включи раньше в «ℹ️ Статус»."
            if ok
            else "Пауза доступна только при активной подписке."
        )
        await cq.answer()

    @router.callback_query(F.data == "sub_resume")
    async def cb_resume(cq: CallbackQuery) -> None:
        ok = await store.resume(cq.from_user.id, _utcnow())
        await cq.message.edit_text(
            "▶️ Подписка возобновлена — снова присылаю варианты." if ok else "Подписка не на паузе."
        )
        await cq.answer()

    return router


# ── helpers ────────────────────────────────────────────────────────────────
def _main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_LIST)],
            [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_BUY)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _metro_prompt(selected: list[str]) -> str:
    if selected:
        return (
            "✅ Выбрано: <b>" + ", ".join(selected) + "</b>\n\n"
            "Тап по станции — убрать.\n"
            "Ещё одну — напишите название.\n"
            "Готово — кнопкой ниже."
        )
    return (
        "Тапните нужную станцию.\n"
        "Другую — напишите название."
    )


def _kb_metro(matches: list[str], selected: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=("✅ " if m in selected else "") + m, callback_data=f"m_add:{i}"
            )
        ]
        for i, m in enumerate(matches)
    ]
    rows.append([InlineKeyboardButton(text="Готово ✓", callback_data="m_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_rooms(selected: list[str]) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text=("✅ " if key in selected else "") + label, callback_data=f"r_tg:{key}"
        )
        for key, label, _ in ROOM_OPTIONS
    ]
    done = [InlineKeyboardButton(text="Готово ✓", callback_data="r_done")]
    return InlineKeyboardMarkup(inline_keyboard=[row, done])


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


def _kb_intervals() -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text=f"{m} мин" if m < 60 else f"{m // 60} ч", callback_data=f"int:{m}"
        )
        for m in INTERVALS
        if MIN_INTERVAL_MIN <= m <= MAX_INTERVAL_MIN
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def _kb_renovation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"rv:{key}")]
            for key, label, _ in RENOVATION_OPTIONS
        ]
    )


def _renovation_filter_label(renovation_min: str) -> str:
    """renovation_min → подпись для описания фильтра."""
    return {
        "simple": "без убитого",
        "modern": "современный/евро",
        "designer": "только дизайнерский",  # legacy — старые фильтры
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
    if flt.max_metro_min:
        parts.append(f"до метро: ≤{flt.max_metro_min} мин")
    if flt.renovation_min:
        parts.append("ремонт: " + _renovation_filter_label(flt.renovation_min))
    if flt.no_commission:
        parts.append("без посредников")
    parts.append(f"проверка каждые {flt.clamp_interval()} мин")
    return "\n".join(parts)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
