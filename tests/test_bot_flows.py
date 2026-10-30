"""Thin Telegram handlers, tested with in-memory FSM and no network."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from rentradar.config import Settings
from rentradar.personal import PersonalStore
from rentradar.personal.bot import NewFilter, build_router
from rentradar.personal.metro import STATIONS, neighbours, search_stations
from rentradar.storage import SqlStorage

NOW = datetime(2026, 10, 25, 9, 17)


def message(uid=1, text=""):
    msg = Mock(spec=Message)
    msg.from_user = SimpleNamespace(id=uid, username=None, first_name="Друг")
    msg.text = text
    msg.answer = AsyncMock()
    msg.edit_text = AsyncMock()
    return msg


def callback(data):
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=1), message=message(), answer=AsyncMock()
    )


def handler(router, name, kind="message"):
    return next(h.callback for h in getattr(router, kind).handlers if h.callback.__name__ == name)


def buttons(msg):
    call = msg.edit_text.call_args or msg.answer.call_args
    return [b for row in call.kwargs["reply_markup"].inline_keyboard for b in row]


@pytest.fixture
async def env(tmp_path, monkeypatch):
    import rentradar.personal.bot as module

    monkeypatch.setattr(module, "_utcnow", lambda: NOW)
    storage = SqlStorage(f"sqlite+aiosqlite:///{(tmp_path / 'bot.db').as_posix()}")
    await storage.init(migration_now=NOW)
    ps = PersonalStore(storage)
    router = build_router(
        ps,
        Settings(
            _env_file=None,
            public_channel="@testchannel",
            admin_id=9,
            yookassa_provider_token="unused",
            admin_contact="@support",
        ),
    )
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=10, chat_id=1, user_id=1))
    yield ps, router, state
    await storage.dispose()


async def test_start_failed_channel_and_retry_do_not_grant(env):
    ps, router, _ = env
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(side_effect=RuntimeError("Telegram unavailable"))
    )
    msg = message(text="/start")
    await handler(router, "start")(msg, bot)
    assert not await ps.is_active(1, NOW)
    cq = callback("check_sub")
    await handler(router, "cb_check_sub", "callback_query")(cq, bot)
    assert not await ps.is_active(1, NOW)
    bot.get_chat_member.side_effect = None
    bot.get_chat_member.return_value = SimpleNamespace(status="restricted", is_member=False)
    await handler(router, "cmd_bonus")(msg, bot)
    assert not await ps.is_active(1, NOW)
    bot.get_chat_member.return_value = SimpleNamespace(status="member")
    await handler(router, "cb_check_sub", "callback_query")(cq, bot)
    assert (await ps.access(1, NOW)).trial_until == NOW + timedelta(hours=24)
    await handler(router, "cmd_bonus")(msg, bot)
    await handler(router, "start")(msg, bot)
    assert (await ps.access(1, NOW)).trial_until == NOW + timedelta(hours=24)


async def test_no_channel_development_mode(env):
    ps, _, _ = env
    router = build_router(ps, Settings(_env_file=None, public_channel=""))
    bot = SimpleNamespace(get_chat_member=AsyncMock())
    await handler(router, "start")(message(text="/start"), bot)
    assert await ps.is_active(1, NOW)
    bot.get_chat_member.assert_not_called()


async def test_manual_tariffs_even_with_old_provider_token(env):
    ps, router, _ = env
    await ps.get_or_create_subscriber(1)
    msg = message()
    await handler(router, "cmd_buy")(msg)
    plans = buttons(msg)
    assert len([b for b in plans if b.callback_data.startswith("tariff:")]) == 4
    assert any(b.callback_data == "nav:menu" for b in plans)
    for code, _hours, price in [
        ("24h", 24, 49),
        ("3d", 72, 129),
        ("7d", 168, 249),
        ("30d", 720, 590),
    ]:
        assert any(b.callback_data == f"tariff:{code}" and str(price) in b.text for b in plans)
        cq = callback(f"tariff:{code}")
        await handler(router, "choose_tariff", "callback_query")(cq)
        text = cq.message.answer.call_args.args[0]
        from rentradar.personal.access import TARIFFS
        assert TARIFFS[code].duration in text and str(price) in text
        assert "час" not in text and " ч " not in text
        button = next(b for b in plans if b.callback_data == f"tariff:{code}")
        assert TARIFFS[code].duration in button.text
        assert " ч " not in button.text
        assert "https://t.me/support" in text and "<code>1</code>" in text
    assert not await ps.is_active(1, NOW)
    pcq = SimpleNamespace(answer=AsyncMock())
    await handler(router, "pre_checkout", "pre_checkout_query")(pcq)
    assert pcq.answer.call_args.kwargs["ok"] is False
    await handler(router, "legacy_payment")(msg)
    assert not await ps.is_active(1, NOW)


async def test_paid_command_arguments_and_admin_gate(env):
    ps, router, _ = env
    await ps.get_or_create_subscriber(1)
    bot = SimpleNamespace(send_message=AsyncMock())
    fn = handler(router, "manual_paid")
    await fn(message(uid=1, text="/paid 1 3d bad"), bot)
    for text in (
        "/paid",
        "/paid abc 3d op",
        "/paid 1 3d",
        "/paid 1 3d op extra",
        "/paid 1 bad op",
        "/paid 999 3d op",
    ):
        await fn(message(uid=9, text=text), bot)
    assert await ps.paid_until_of(1) is None
    await fn(message(uid=9, text="/paid 1 3d ok"), bot)
    assert await ps.paid_until_of(1) == NOW + timedelta(hours=72)


async def test_paid_notifies_buyer_and_referrer_and_reminds_receipt(env):
    ps, router, _ = env
    await ps.get_or_create_subscriber(2)
    await ps.get_or_create_subscriber(1, referred_by=2)
    bot = SimpleNamespace(send_message=AsyncMock())
    admin = message(uid=9, text="/paid 1 3d op_1")
    await handler(router, "manual_paid")(admin, bot)
    assert "«Мой налог»: 129 ₽" in admin.answer.call_args.args[0]
    sent = {c.args[0]: c for c in bot.send_message.call_args_list}
    assert "Оплата подтверждена" in sent[1].args[1] and sent[1].kwargs["reply_markup"]
    assert "+48 часов" in sent[2].args[1]


async def test_metro_groups_toggle_remove_stale_and_cancel(env):
    _, router, state = env
    await state.set_state(NewFilter.metro)
    await state.update_data(metros_sel=[])
    search = handler(router, "metro_search")
    act = handler(router, "metro_action", "callback_query")
    msg = message(text="Рязанский проспект")
    await search(msg, state)
    old_search = buttons(msg)[0].callback_data
    root = callback(old_search)
    await act(root, state)
    data = await state.get_data()
    assert data["metro_draft"] == ["Рязанский проспект · 7"]
    root_button = next(b for b in buttons(root.message) if "Рязанский проспект" in b.text)
    assert "✅" in root_button.text
    toggle = callback(root_button.callback_data)
    await act(toggle, state)
    assert (await state.get_data())["metro_draft"] == []
    after = await state.get_data()
    await act(toggle, state)  # replay must not re-add the main station
    assert await state.get_data() == after
    neighbour_button = next(b for b in buttons(toggle.message) if "Выхино" in b.text)
    pick = callback(neighbour_button.callback_data)
    await act(pick, state)
    done = callback(next(b.callback_data for b in buttons(pick.message) if b.text == "Готово"))
    await act(done, state)
    assert (await state.get_data())["metros_sel"] == ["Выхино · 7"]
    more = callback(
        next(b.callback_data for b in buttons(done.message) if b.text == "Добавить ещё станции")
    )
    await act(more, state)
    await search(message(text="Кузьминки"), state)
    old = await state.get_data()
    await act(callback(old_search), state)
    assert await state.get_data() == old
    current = callback(f"m:{old['metro_version']}:{old['metro_ids'][0]}")
    await act(current, state)
    # Previously selected station in overlapping group remains selected.
    assert "Выхино · 7" in (await state.get_data())["metro_draft"]
    done2 = callback(next(b.callback_data for b in buttons(current.message) if b.text == "Готово"))
    await act(done2, state)
    data = await state.get_data()
    assert sorted(data["metros_sel"]) == ["Выхино · 7", "Кузьминки · 7"]
    remove = callback(next(b.callback_data for b in buttons(done2.message) if "Выхино" in b.text))
    await act(remove, state)
    assert (await state.get_data())["metros_sel"] == ["Кузьминки · 7"]
    finish = callback(
        next(b.callback_data for b in buttons(remove.message) if b.text == "Продолжить")
    )
    await act(finish, state)
    assert await state.get_state() == NewFilter.max_metro.state
    assert (await state.get_data())["metros"] == ["Кузьминки · 7"]
    await handler(router, "cancel")(message(), state)
    assert await state.get_data() == {}
    await handler(router, "stale_metro", "callback_query")(root)
    assert await state.get_state() is None


def test_local_topology_and_unambiguous_ids():
    exact = search_stations("Рязанский проспект")
    assert len(exact) == 1 and exact[0].line == "7"
    assert search_stations("рязаский проспект")[0] == exact[0]
    group = neighbours(exact[0].id)
    assert {s.name for s, _ in group} == {
        "Рязанский проспект",
        "Кузьминки",
        "Текстильщики",
        "Волгоградский проспект",
        "Выхино",
        "Лермонтовский проспект",
        "Жулебино",
    }
    end = search_stations("Котельники")[0]
    assert len(neighbours(end.id)) == 4
    ring = next(s for s in search_stations("Парк культуры") if s.line == "5")
    assert len(neighbours(ring.id)) == 7
    assert "Киевская" in {s.name for s, _ in neighbours(ring.id)}
    branch = next(s for s in search_stations("Киевская") if s.line == "4")
    branched = neighbours(branch.id)
    assert len(branched) > 7
    assert all(s.line == "4" for s, _ in branched)
    assert {s.name for s, _ in branched} >= {"Москва-Сити", "Александровский сад", "Фили"}
    assert len({s.id for s, _ in branched}) == len(branched)
    assert {s.line for s in search_stations("Арбатская")} == {"3", "4"}
    assert len(STATIONS) > 250


async def test_empty_metro_choice_is_explicit_and_filter_cancelled(env):
    ps, router, state = env
    from rentradar.personal import UserFilter

    await ps.get_or_create_subscriber(1)
    await ps.upsert_filter(UserFilter(user_id=1, metros=["Выхино"]), now=NOW)
    await state.set_state(NewFilter.metro)
    await state.update_data(metros_sel=[])
    msg = message(text="Рязанский проспект")
    await handler(router, "metro_search")(msg, state)
    act = handler(router, "metro_action", "callback_query")
    select = callback(buttons(msg)[0].callback_data)
    await act(select, state)
    remove = callback(next(b.callback_data for b in buttons(select.message) if "✅" in b.text))
    await act(remove, state)
    done = callback(next(b.callback_data for b in buttons(remove.message) if b.text == "Готово"))
    await act(done, state)
    assert "без ограничения по метро" in done.message.edit_text.call_args.args[0]
    finish = callback(
        next(b.callback_data for b in buttons(done.message) if b.text == "Продолжить")
    )
    await act(finish, state)
    assert (await state.get_data())["metros"] == []
    assert await state.get_state() == NewFilter.max_metro.state
    await handler(router, "cancel")(message(), state)
    filters = await ps.list_filters(1)
    assert len(filters) == 1 and filters[0][1].metros == ["Выхино"]


async def test_grant_arguments_and_preserving_remainder(env):
    ps, router, _ = env
    await ps.get_or_create_subscriber(1)
    await ps.grant_hours(1, NOW, 10)
    fn = handler(router, "grant")
    for text in ("/grant", "/grant 1 0", "/grant 1 -1", "/grant 1 ²", "/grant 1 2 extra"):
        await fn(message(uid=9, text=text))
    assert await ps.paid_until_of(1) == NOW + timedelta(hours=10)
    await fn(message(uid=1, text="/grant 1 3"))
    assert await ps.paid_until_of(1) == NOW + timedelta(hours=10)
    await fn(message(uid=9, text="/grant 1 3"))
    assert await ps.paid_until_of(1) == NOW + timedelta(hours=82)


async def test_visible_inline_menu_uses_clicking_user_not_bot(env):
    ps, router, state = env
    await ps.get_or_create_subscriber(1)
    await ps.verify_channel(1, NOW)
    from rentradar.personal import UserFilter

    await ps.upsert_filter(UserFilter(user_id=1, name="correct user"), now=NOW)
    bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="testbot")))
    nav = handler(router, "navigate", "callback_query")
    for action in ("menu", "filters", "status", "buy", "invite", "support", "new"):
        cq = callback("nav:" + action)
        cq.message.from_user.id = 999  # Telegram's message sender is the bot.
        await nav(cq, state, bot)
        if action == "menu":
            keys = buttons(cq.message)
            assert len(keys) == 6
            assert {b.callback_data for b in keys} == {
                "nav:new",
                "nav:filters",
                "nav:status",
                "nav:buy",
                "nav:invite",
                "nav:support",
            }
        elif action == "filters":
            assert "correct user" in cq.message.answer.call_args.args[0]
        elif action == "status":
            assert "✅" in cq.message.answer.call_args.args[0]
        elif action == "invite":
            assert "start=ref_1" in cq.message.answer.call_args.args[0]
    assert await state.get_state() == NewFilter.rooms.state


async def test_native_telegram_menu_registered_on_startup(env):
    _, router, _ = env
    bot = SimpleNamespace(set_my_commands=AsyncMock(), set_chat_menu_button=AsyncMock())
    await router.emit_startup(bot=bot)
    commands = bot.set_my_commands.call_args.args[0]
    assert {c.command for c in commands} >= {"menu", "new", "status", "myfilters", "buy"}
    assert bot.set_my_commands.call_args.kwargs["scope"].type == "all_private_chats"
    assert bot.set_chat_menu_button.call_args.kwargs["menu_button"].type == "commands"


async def test_chukhlinka_ui_selection(env):
    _, router, state = env
    await state.set_state(NewFilter.metro)
    msg = message(text="Чухлинка")
    await handler(router, "metro_search")(msg, state)
    station_button = buttons(msg)[0]
    assert "Чухлинка" in station_button.text and "D4" in station_button.text
    act = handler(router, "metro_action", "callback_query")
    pick = callback(station_button.callback_data)
    await act(pick, state)
    done = callback(next(b.callback_data for b in buttons(pick.message) if b.text == "Готово"))
    await act(done, state)
    finish = callback(
        next(b.callback_data for b in buttons(done.message) if b.text == "Продолжить")
    )
    await act(finish, state)
    assert (await state.get_data())["metros"] == ["Чухлинка · D4"]
