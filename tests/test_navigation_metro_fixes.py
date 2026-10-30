"""Deletion navigation and preset walking-time filters, without Telegram network calls."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from rentradar.config import Settings
from rentradar.personal import PersonalStore, UserFilter, matches
from rentradar.personal.bot import NewFilter, _describe, build_router
from rentradar.personal.navigation import main_menu
from rentradar.storage import SqlStorage

NOW = datetime(2026, 10, 26, 10)


def message(text=""):
    msg = Mock(spec=Message)
    msg.from_user = SimpleNamespace(id=999, username="bot", first_name="Bot")
    msg.text = text
    msg.answer = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    return msg


def callback(data):
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=1), message=message(), answer=AsyncMock()
    )


def handler(router, name, kind="callback_query"):
    return next(h.callback for h in getattr(router, kind).handlers if h.callback.__name__ == name)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    import rentradar.personal.bot as module

    monkeypatch.setattr(module, "_utcnow", lambda: NOW)
    storage = SqlStorage(f"sqlite+aiosqlite:///{(tmp_path / 'navigation.db').as_posix()}")
    await storage.init(migration_now=NOW)
    personal = PersonalStore(storage)
    await personal.get_or_create_subscriber(1)
    await personal.grant_hours(1, NOW, 24)
    router = build_router(personal, Settings(_env_file=None, public_channel=""))
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=10, chat_id=1, user_id=1))
    yield personal, router, state
    await state.storage.close()
    await storage.dispose()


@pytest.mark.parametrize("remaining", [0, 1])
async def test_delete_last_or_one_of_multiple_filters_restores_working_menu(env, remaining):
    personal, router, state = env
    delete_id = await personal.upsert_filter(UserFilter(user_id=1, name="Удаляемый"))
    for _ in range(remaining):
        await personal.upsert_filter(UserFilter(user_id=1, name="Оставшийся"))
    await state.set_state(NewFilter.max_metro)
    await state.update_data(price_max=50000)
    cq = callback(f"del:{delete_id}")
    await handler(router, "delete_filter")(cq, state)
    assert cq.message.edit_text.call_args.args == ("Фильтр удалён",)
    assert cq.message.edit_text.call_args.kwargs["reply_markup"] == main_menu()
    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert len(await personal.list_filters(1)) == remaining

    # Both emitted buttons call existing navigation with the clicking user's ID.
    bot = SimpleNamespace(get_chat_member=AsyncMock())
    navigation = handler(router, "navigate")
    filters = callback("nav:filters")
    await navigation(filters, state, bot)
    text = filters.message.answer.call_args.args[0]
    assert ("Оставшийся" if remaining else "пока нет фильтров") in text
    new = callback("nav:new")
    await navigation(new, state, bot)
    assert await state.get_state() == NewFilter.rooms.state
    assert "Сколько комнат" in new.message.answer.call_args.args[0]


@pytest.mark.parametrize("kind", ["missing", "foreign", "invalid"])
async def test_failed_delete_never_confirms_success(env, kind):
    personal, router, state = env
    foreign_id = await personal.upsert_filter(UserFilter(user_id=2))
    fid = {"missing": "99999", "foreign": str(foreign_id), "invalid": "invalid"}[kind]
    await state.set_state(NewFilter.rooms)
    await state.update_data(rooms_sel=["1"])
    cq = callback(f"del:{fid}")
    await handler(router, "delete_filter")(cq, state)
    assert not any("Фильтр удалён" in str(call) for call in cq.message.edit_text.call_args_list)
    assert len(await personal.list_filters(2)) == 1
    assert await state.get_state() == NewFilter.rooms.state
    assert (await state.get_data())["rooms_sel"] == ["1"]


async def test_storage_failure_never_sends_deletion_confirmation(env, monkeypatch):
    personal, router, state = env
    monkeypatch.setattr(personal, "delete_filter", AsyncMock(side_effect=RuntimeError("db")))
    cq = callback("del:1")
    with pytest.raises(RuntimeError, match="db"):
        await handler(router, "delete_filter")(cq, state)
    cq.message.edit_text.assert_not_called()


async def test_metro_step_offers_only_presets_and_explains_walking_and_skip(env):
    _, router, state = env
    await state.set_state(NewFilter.metro)
    await state.update_data(metros_sel=["Парк культуры"])
    msg = message("Пропустить")
    await handler(router, "metro_search", "message")(msg, state)
    assert await state.get_state() == NewFilter.max_metro.state
    text = msg.answer.call_args.args[0]
    assert "минут пешком" in text and "близость метро не важна" in text
    keyboard = msg.answer.call_args.kwargs["reply_markup"]
    buttons = [b for row in keyboard.inline_keyboard for b in row
               if b.callback_data.startswith(("skip:", "maxmetro:"))]
    assert [b.text for b in buttons] == [
        "Пропустить", "До 5 минут", "До 10 минут", "До 15 минут", "До 20 минут",
    ]
    assert [b.callback_data for b in buttons] == [
        "skip:maxmetro", "maxmetro:5", "maxmetro:10", "maxmetro:15", "maxmetro:20",
    ]


async def test_pause_has_unfreeze_and_menu_and_resume_has_main_menu(env):
    personal, router, _ = env
    pause = callback("sub_pause")
    await handler(router, "cb_pause")(pause)
    assert await personal.is_paused(1, NOW)
    keyboard = pause.message.edit_text.call_args.kwargs["reply_markup"]
    actions = {b.callback_data for row in keyboard.inline_keyboard for b in row}
    assert actions == {"sub_resume", "nav:menu"}
    resume = callback("sub_resume")
    await handler(router, "cb_resume")(resume)
    assert not await personal.is_paused(1, NOW)
    assert resume.message.edit_text.call_args.kwargs["reply_markup"] == main_menu()


async def test_status_offers_pause_only_when_paid_time_can_freeze(env):
    personal, router, state = env
    await personal.get_or_create_subscriber(5)
    await personal.verify_channel_with_reward(5, NOW)  # trial only: nothing to freeze

    async def status_actions(uid):
        cq = callback("nav:status")
        cq.from_user = SimpleNamespace(id=uid)
        await handler(router, "navigate")(cq, state, SimpleNamespace())
        keyboard = cq.message.answer.call_args.kwargs["reply_markup"]
        return {b.callback_data for row in keyboard.inline_keyboard for b in row}

    assert await status_actions(5) == {"nav:menu"}
    assert await status_actions(1) == {"sub_pause", "nav:menu"}
    assert await personal.pause(1, NOW)
    assert await status_actions(1) == {"sub_resume", "nav:menu"}


@pytest.mark.parametrize("action", ["support", "invite", "status", "buy", "filters"])
async def test_menu_screens_offer_working_back_to_menu(env, action):
    _, router, state = env
    bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="test_bot")))
    cq = callback(f"nav:{action}")
    await handler(router, "navigate")(cq, state, bot)
    keyboard = cq.message.answer.call_args.kwargs["reply_markup"]
    back = next(b for row in keyboard.inline_keyboard for b in row if "Назад" in b.text)
    assert back.callback_data == "nav:menu"
    await state.set_state(NewFilter.price)
    await state.update_data(price_max=70000)
    menu = callback(back.callback_data)
    await handler(router, "navigate")(menu, state, bot)
    assert await state.get_state() is None and await state.get_data() == {}
    assert menu.message.answer.call_args.kwargs["reply_markup"] == main_menu()


@pytest.mark.parametrize("step,previous", [
    ("price", "rooms"), ("metro", "price"), ("max_metro", "metro"),
    ("renovation", "max_metro"), ("no_commission", "renovation"),
    ("interval", "no_commission"), ("backlog", "interval"),
])
async def test_filter_back_keeps_draft_and_rejects_stale_button(env, step, previous):
    _, router, state = env
    draft = dict(city="Москва", rooms_sel=["1"], rooms=[1], price_max=70000,
                 metros_sel=["Парк культуры · 1"], max_metro_min=10, no_commission=True,
                 interval_min=30, metro_phase="search")
    await state.set_state(getattr(NewFilter, step))
    await state.update_data(draft)
    cq = callback(f"filter_back:{step}")
    await handler(router, "filter_back")(cq, state)
    assert await state.get_state() == getattr(NewFilter, previous).state
    saved = await state.get_data()
    for key, value in draft.items():
        if key != "metro_phase":
            assert saved[key] == value
    keyboard = cq.message.answer.call_args.kwargs["reply_markup"]
    assert any(b.callback_data == f"filter_back:{previous}"
               for row in keyboard.inline_keyboard for b in row)
    await handler(router, "filter_back")(cq, state)
    assert await state.get_data() == saved
    assert await state.get_state() == getattr(NewFilter, previous).state


async def test_back_to_budget_then_forward_preserves_stations_and_saves_corrected_filter(env):
    personal, router, state = env
    station = "Парк культуры · 1"
    await state.set_state(NewFilter.max_metro)
    await state.update_data(city="Москва", rooms=[1], rooms_sel=["1"], price_max=70000,
                            metros=[station], metros_sel=[station], max_metro_min=10)
    back = handler(router, "filter_back")
    await back(callback("filter_back:max_metro"), state)  # Station summary.
    await back(callback("filter_back:metro"), state)  # Station input.
    await back(callback("filter_back:metro"), state)  # Budget.
    await handler(router, "set_price", "message")(message("до 60000"), state)
    assert (await state.get_data())["metros_sel"] == [station]
    await handler(router, "metro_search", "message")(message("Пропустить"), state)
    await handler(router, "choose_max_metro")(callback("maxmetro:10"), state)
    await handler(router, "set_renovation")(callback("rv:any"), state)
    await handler(router, "set_no_commission")(callback("nc:0"), state)
    await handler(router, "set_interval")(callback("int:30"), state)
    finish = callback("bl:1")
    await handler(router, "set_backlog")(finish, state, SimpleNamespace())
    _, saved = (await personal.list_filters(1))[0]
    assert saved.price_max == 60000 and saved.metros == [station] and saved.rooms == [1]
    assert saved.max_metro_min == 10 and await state.get_state() is None
    assert finish.message.edit_text.call_args.kwargs["reply_markup"] == main_menu()


async def test_first_filter_step_back_returns_to_menu_without_saving(env):
    personal, router, state = env
    await state.set_state(NewFilter.rooms)
    await state.update_data(rooms_sel=["1"])
    cq = callback("filter_back:rooms")
    await handler(router, "filter_back")(cq, state)
    assert await state.get_state() is None and await state.get_data() == {}
    assert await personal.list_filters(1) == []
    assert cq.message.answer.call_args.kwargs["reply_markup"] == main_menu()


@pytest.mark.parametrize("limit", [5, 10, 15, 20])
async def test_preset_saves_persists_and_applies_as_inclusive_maximum(env, sample_listing, limit):
    personal, router, state = env
    await state.set_state(NewFilter.max_metro)
    await state.update_data(city="Москва", rooms=[1], price_max=60000, metros=["Парк культуры"])
    cq = callback(f"maxmetro:{limit}")
    await handler(router, "choose_max_metro")(cq, state)
    assert await state.get_state() == NewFilter.renovation.state
    assert (await state.get_data())["max_metro_min"] == limit
    assert f"до {limit} минут пешком" in cq.message.edit_text.call_args.args[0]

    await handler(router, "set_renovation")(callback("rv:any"), state)
    await handler(router, "set_no_commission")(callback("nc:0"), state)
    await handler(router, "set_interval")(callback("int:30"), state)
    await handler(router, "set_backlog")(callback("bl:1"), state, SimpleNamespace())
    assert await state.get_state() is None
    _, saved = (await personal.list_filters(1))[0]
    assert saved.max_metro_min == limit and saved.price_max == 60000 and saved.rooms == [1]
    assert f"до {limit} минут пешком" in _describe(saved)
    for minutes in (1, limit - 1, limit):
        assert matches(sample_listing.model_copy(update={"metro_distance_min": minutes}), saved)
    for minutes in (limit + 1, None):
        assert not matches(sample_listing.model_copy(update={"metro_distance_min": minutes}), saved)


async def test_skip_only_clears_time_limit_and_persists_other_conditions(env, sample_listing):
    personal, router, state = env
    criteria = dict(city="Москва", rooms=[1], price_min=40000, price_max=60000,
                    metros=["Парк культуры"], max_metro_min=5)
    await state.set_state(NewFilter.max_metro)
    await state.update_data(criteria)
    cq = callback("skip:maxmetro")
    await handler(router, "skip_max_metro")(cq, state)
    assert await state.get_state() == NewFilter.renovation.state
    assert await state.get_data() == {**criteria, "max_metro_min": None}
    await handler(router, "set_renovation")(callback("rv:any"), state)
    await handler(router, "set_no_commission")(callback("nc:0"), state)
    await handler(router, "set_interval")(callback("int:30"), state)
    await handler(router, "set_backlog")(callback("bl:1"), state, SimpleNamespace())
    _, saved = (await personal.list_filters(1))[0]
    assert saved.max_metro_min is None and saved.metros == ["Парк культуры"]
    assert matches(sample_listing.model_copy(update={"metro_distance_min": None}), saved)
    assert matches(sample_listing.model_copy(update={"metro_distance_min": 50}), saved)
    assert not matches(sample_listing.model_copy(update={"price": 70000}), saved)
    assert not matches(sample_listing.model_copy(update={"rooms": 2}), saved)
    assert not matches(sample_listing.model_copy(update={"metro": "Тверская"}), saved)


async def test_manual_input_does_not_create_an_arbitrary_limit(env):
    _, router, state = env
    await state.set_state(NewFilter.max_metro)
    await state.update_data(price_max=60000)
    msg = message("4")
    await handler(router, "set_max_metro", "message")(msg, state)
    assert await state.get_state() == NewFilter.max_metro.state
    assert await state.get_data() == {"price_max": 60000}
    assert msg.answer.call_args.kwargs["reply_markup"].inline_keyboard
    cq = callback("maxmetro:4")
    await handler(router, "choose_max_metro")(cq, state)
    assert await state.get_state() == NewFilter.max_metro.state
    assert await state.get_data() == {"price_max": 60000}


async def test_legacy_arbitrary_limit_survives_storage_and_matches(env, sample_listing):
    personal, _, _ = env
    fid = await personal.upsert_filter(UserFilter(user_id=1, max_metro_min=4, price_max=60000))
    _, saved = await personal.get_filter(fid)
    assert saved.max_metro_min == 4
    assert matches(sample_listing.model_copy(update={"metro_distance_min": 4}), saved)
    assert not matches(sample_listing.model_copy(update={"metro_distance_min": 5}), saved)
    assert not matches(sample_listing.model_copy(update={"metro_distance_min": None}), saved)
