"""Access, migration and money invariants on isolated SQLite databases."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from rentradar.cli import _lifecycle_nudge
from rentradar.config import Settings
from rentradar.personal import PersonalStore, UserFilter
from rentradar.personal.access import TARIFFS
from rentradar.personal.bot import build_router
from rentradar.storage import SqlStorage
from rentradar.storage.orm import ManualPaymentRow, SubscriberRow

NOW = datetime(2026, 10, 25, 9, 17)


@pytest.fixture
async def env(tmp_path):
    storage = SqlStorage(f"sqlite+aiosqlite:///{(tmp_path / 'access.db').as_posix()}")
    await storage.init(migration_now=NOW)
    ps = PersonalStore(storage)
    await ps.get_or_create_subscriber(1)
    yield storage, ps
    await storage.dispose()


async def pay(ps, code="3d", op="one", user=1, now=NOW):
    return await ps.confirm_payment(user, code, op, 9, {9}, now)


@pytest.mark.parametrize(
    "code,hours,price", [("24h", 24, 49), ("3d", 72, 129), ("7d", 168, 249), ("30d", 720, 590)]
)
async def test_catalogue_and_credit(env, code, hours, price):
    _, ps = env
    result = await pay(ps, code)
    assert (result.hours, result.price_rub) == (hours, price)
    assert result.result_until == NOW + timedelta(hours=hours)
    assert TARIFFS[code].hours == hours


async def test_trial_exact_and_legacy_counter_irrelevant(env):
    storage, ps = env
    await ps.increment_free_sends(1, 123)
    assert not await ps.is_active(1, NOW)
    assert await ps.verify_channel(1, NOW)
    assert not await ps.pause(1, NOW)
    assert await ps.is_active(1, NOW + timedelta(hours=24) - timedelta(microseconds=1))
    assert not await ps.is_active(1, NOW + timedelta(hours=24))
    await storage.init(migration_now=NOW + timedelta(days=10))
    assert not await ps.verify_channel(1, NOW + timedelta(days=10))
    assert (await ps.access(1, NOW)).trial_until == NOW + timedelta(hours=24)


async def test_extend_remaining_and_expired(env):
    _, ps = env
    await ps.grant_hours(1, NOW, 10)
    await pay(ps)
    assert (await ps.access(1, NOW)).until == NOW + timedelta(hours=82)
    await pay(ps, "24h", "after", now=NOW + timedelta(days=5))
    assert (await ps.access(1, NOW + timedelta(days=5))).until == NOW + timedelta(days=6)


async def test_payment_and_gift_on_pause(env):
    _, ps = env
    await ps.grant_hours(1, NOW, 10)
    await ps.pause(1, NOW)
    later = NOW + timedelta(hours=5)
    await pay(ps, now=later)
    await ps.grant_hours(1, later, 12)
    assert not await ps.is_active(1, later)
    assert (await ps.access(1, later)).frozen_seconds == 94 * 3600
    await ps.resume(1, later)
    assert (await ps.access(1, later)).until == later + timedelta(hours=94)


@pytest.mark.parametrize("resume_hours", [18, 25])
async def test_purchase_during_trial_pause_never_freezes_trial(env, resume_hours):
    _, ps = env
    await ps.verify_channel(1, NOW)
    buy_at = NOW + timedelta(hours=14)  # 10 trial hours left
    await pay(ps, now=buy_at)
    assert (await ps.access(1, buy_at)).until == NOW + timedelta(hours=96)
    assert await ps.pause(1, buy_at)
    assert (await ps.access(1, buy_at)).frozen_seconds == 72 * 3600
    assert await ps.is_active(1, NOW + timedelta(hours=23))
    assert not await ps.is_active(1, NOW + timedelta(hours=24))
    resumed = NOW + timedelta(hours=resume_hours)
    assert await ps.resume(1, resumed)
    expected = NOW + timedelta(hours=max(24, resume_hours) + 72)
    assert (await ps.access(1, resumed)).until == expected
    assert (await ps.access(1, resumed)).trial_until == NOW + timedelta(hours=24)


async def test_trial_then_other_access_and_reminders(env):
    _, ps = env
    await ps.verify_channel(1, NOW)
    await ps.grant_hours(1, NOW, 12)
    after = NOW + timedelta(hours=24)
    assert await ps.is_active(1, after)
    row = (await ps.lifecycle_candidates())[0]
    kind, _ = _lifecycle_nudge(row, after, Settings(_env_file=None))
    assert kind != "trial"
    assert (await ps.access(1, after)).until == NOW + timedelta(hours=36)


async def test_idempotency_conflict_and_concurrent_payments(env):
    storage, ps = env
    # Two stores, separate connections: the database, not a process-local lock, serializes.
    other = PersonalStore(storage)
    await asyncio.gather(pay(ps), pay(other))
    assert await ps.paid_until_of(1) == NOW + timedelta(hours=72)
    original = await pay(ps)
    assert original.result_until == NOW + timedelta(hours=72)
    await ps.get_or_create_subscriber(2)
    for code, uid in [("7d", 1), ("3d", 2)]:
        with pytest.raises(ValueError):
            await pay(ps, code, user=uid)
    await asyncio.gather(pay(ps, "24h", "two"), pay(other, "7d", "three"))
    assert await ps.paid_until_of(1) == NOW + timedelta(hours=72 + 24 + 168)
    async with storage._session() as session:
        assert await session.scalar(select(func.count()).select_from(ManualPaymentRow)) == 3


async def test_payment_validation(env):
    _, ps = env
    for uid, code, op in [
        (999, "24h", "ok"),
        (1, "bad", "ok"),
        (1, "24h", ""),
        (-1, "24h", "ok"),
        (1, "24h", "space in id"),
    ]:
        with pytest.raises(ValueError):
            await pay(ps, code, op, uid)
    with pytest.raises(PermissionError):
        await ps.confirm_payment(1, "24h", "x", 7, {9}, NOW)
    assert await ps.paid_until_of(1) is None


@pytest.mark.parametrize("payment_first", [True, False])
async def test_independent_referral_rewards_and_gift(env, payment_first):
    _, ps = env
    await ps.get_or_create_subscriber(2, referred_by=1)
    await ps.grant_hours(2, NOW, 10)
    assert await ps.paid_until_of(1) is None  # /grant is not payment
    if payment_first:
        await pay(ps, user=2)
        assert await ps.paid_until_of(1) == NOW + timedelta(hours=48)
    await ps.verify_channel(2, NOW)
    await asyncio.gather(*(ps.upsert_filter(UserFilter(user_id=2), now=NOW) for _ in range(3)))
    if not payment_first:
        assert await ps.paid_until_of(1) == NOW + timedelta(hours=12)
        await pay(ps, user=2)
    await ps.verify_channel(2, NOW)
    await pay(ps, op="second", user=2)
    assert await ps.paid_until_of(1) == NOW + timedelta(hours=60)


async def test_filter_before_verification_and_parallel_different_friends(env):
    _, ps = env
    for uid in (2, 3):
        await ps.get_or_create_subscriber(uid, referred_by=1)
        await ps.upsert_filter(UserFilter(user_id=uid), now=NOW)
    assert await ps.paid_until_of(1) is None
    await asyncio.gather(
        ps.verify_channel(2, NOW),
        ps.verify_channel(3, NOW),
        pay(ps, user=2),
        pay(ps, user=3, op="other"),
    )
    assert await ps.paid_until_of(1) == NOW + timedelta(hours=120)


async def test_referrer_fixed_at_registration(env):
    _, ps = env
    await ps.get_or_create_subscriber(2)
    assert (await ps.get_or_create_subscriber(2, referred_by=1)).referred_by is None
    await ps.get_or_create_subscriber(3, referred_by=1)
    assert (await ps.get_or_create_subscriber(3, referred_by=2)).referred_by == 1
    assert (await ps.get_or_create_subscriber(4, referred_by=4)).referred_by is None


async def test_atomic_payment_rollback(env, monkeypatch):
    storage, ps = env
    await ps.get_or_create_subscriber(2, referred_by=1)
    import rentradar.personal.store as module

    real_add = module.add_hours

    def fail_referrer(row, *args):
        real_add(row, *args)
        if row.telegram_id == 1:
            raise RuntimeError("simulate failure after both changes")

    monkeypatch.setattr(module, "add_hours", fail_referrer)
    with pytest.raises(RuntimeError):
        await pay(ps, user=2)
    assert await ps.paid_until_of(1) is None
    assert await ps.paid_until_of(2) is None
    async with storage._session() as session:
        assert await session.get(ManualPaymentRow, "one") is None
        assert not (await session.get(SubscriberRow, 2)).referral_credited
    monkeypatch.setattr(module, "add_hours", real_add)
    await pay(ps, user=2)
    assert await ps.paid_until_of(1) == NOW + timedelta(hours=48)


async def test_migration_snapshot_once_preserves_old_data(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as con:
        con.execute("""CREATE TABLE subscribers (
            telegram_id INTEGER PRIMARY KEY, paid_until DATETIME, paused_at DATETIME,
            free_sends_used INTEGER, channel_bonus_claimed BOOLEAN,
            referred_by INTEGER, referral_credited BOOLEAN, referral_activated BOOLEAN)""")
        con.execute("""CREATE TABLE user_filters (
            id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, city TEXT,
            rooms JSON, metros JSON, districts JSON, active BOOLEAN,
            interval_min INTEGER, include_backlog BOOLEAN)""")
        con.execute("""INSERT INTO user_filters VALUES
            (11, 1, 'legacy', 'Москва', '[1]', '["Рязанский проспект"]', '[]', 1, 15, 1)""")
        con.execute("""CREATE TABLE user_sent (id INTEGER PRIMARY KEY, user_id INTEGER,
            content_key TEXT, sent_at DATETIME, UNIQUE(user_id, content_key))""")
        con.execute("INSERT INTO user_sent VALUES (1,1,'delivered',NULL)")
        con.executemany(
            "INSERT INTO subscribers VALUES (?,?,?,?,?,?,?,?)",
            [
                (1, None, None, 0, 1, None, 1, 1),
                (2, NOW + timedelta(hours=10), None, 999, 1, 1, 1, 1),
                (3, NOW + timedelta(hours=10), NOW, 0, 0, 1, 0, 0),
                (4, NOW - timedelta(days=5), None, 4, 0, None, 0, 0),
                (6, NOW - timedelta(hours=26), NOW - timedelta(hours=36), 0, 1, None, 0, 0),
                (7, NOW.strftime("%Y-%m-%d %H:%M:%S.000000"), None, 0, 1, None, 0, 0),
                (8, None, None, None, None, None, None, None),
            ],
        )
    storage = SqlStorage(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        await storage.init(migration_now=NOW)
        ps = PersonalStore(storage)
        filters = await ps.list_filters(1)
        assert len(filters) == 1 and filters[0][0] == 11
        assert filters[0][1].metros == ["Рязанский проспект"]
        assert filters[0][1].interval_min == 15
        assert await ps.was_sent(1, "delivered")
        rows = {r.telegram_id: r for r in await ps.lifecycle_candidates()}
        assert [rows[i].trial_eligible for i in (1, 2, 3, 4)] == [True, False, False, True]
        assert rows[1].channel_bonus_claimed and rows[2].referral_credited
        assert rows[2].referred_by == 1 and rows[3].paused_at == NOW
        assert not rows[6].trial_eligible  # paused remnant, even past wall-clock paid_until
        assert rows[7].trial_eligible  # exactly at expiry, including .000000 storage
        assert rows[8].trial_eligible and rows[8].free_sends_used == 0
        assert rows[8].channel_bonus_claimed is False
        assert rows[8].referral_credited is False
        assert (await ps.access(3, NOW)).frozen_seconds == 10 * 3600
        assert await ps.verify_channel(1, NOW)
        before = await ps.paid_until_of(1)
        await pay(ps, user=2)
        assert await ps.paid_until_of(1) == before  # old rewarded flag survives, no re-credit
        await storage.init(migration_now=NOW + timedelta(days=20))
        assert not await ps.verify_channel(1, NOW + timedelta(days=20))
        assert not await ps.verify_channel(2, NOW + timedelta(days=20))
        assert not await ps.verify_channel(3, NOW + timedelta(days=20))
        assert await ps.verify_channel(4, NOW + timedelta(days=20))
        await ps.get_or_create_subscriber(5)
        assert await ps.verify_channel(5, NOW + timedelta(days=20))
    finally:
        await storage.dispose()


async def test_notification_failure_does_not_repeat_credit(env, monkeypatch):
    _, ps = env
    import rentradar.personal.bot as module

    monkeypatch.setattr(module, "_utcnow", lambda: NOW)
    router = build_router(ps, Settings(_env_file=None, admin_id=9))
    handler = next(
        h.callback for h in router.message.handlers if h.callback.__name__ == "manual_paid"
    )
    from types import SimpleNamespace

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=9), text="/paid 1 3d operation", answer=AsyncMock()
    )
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("blocked")))
    await handler(message, bot)
    await handler(message, bot)
    assert await ps.paid_until_of(1) == NOW + timedelta(hours=72)


async def test_migration_does_not_round_access_boundary(tmp_path):
    path = tmp_path / "precise.db"
    stamp = NOW.replace(microsecond=500001)
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE subscribers (telegram_id INTEGER PRIMARY KEY, paid_until DATETIME)"
        )
        con.executemany(
            "INSERT INTO subscribers VALUES (?, ?)",
            [
                (1, stamp - timedelta(microseconds=1)),
                (2, stamp),
                (3, stamp + timedelta(microseconds=1)),
            ],
        )
    storage = SqlStorage(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        await storage.init(migration_now=stamp)
        rows = {r.telegram_id: r for r in await PersonalStore(storage).lifecycle_candidates()}
        assert rows[1].trial_eligible and rows[2].trial_eligible
        assert not rows[3].trial_eligible
    finally:
        await storage.dispose()
