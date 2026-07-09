"""Тесты хранилища персонального бота: фильтры, подписка/триал, дедуп, выборка."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from rentradar.models import DealType, Listing, PropertyType, Source
from rentradar.personal import PersonalStore, UserFilter
from rentradar.storage import SqlStorage


@pytest.fixture
async def storage(tmp_path):
    st = SqlStorage(f"sqlite+aiosqlite:///{(tmp_path / 'p.db').as_posix()}")
    await st.init()
    yield st
    await st.dispose()


@pytest.fixture
def ps(storage: SqlStorage) -> PersonalStore:
    return PersonalStore(storage)


# ── фильтры ────────────────────────────────────────────────────────────────
async def test_filter_crud(ps: PersonalStore) -> None:
    fid = await ps.upsert_filter(
        UserFilter(user_id=42, name="Студии у метро", rooms=[0, 1], price_max=60000)
    )
    items = await ps.list_filters(42)
    assert len(items) == 1
    got_id, flt = items[0]
    assert got_id == fid
    assert flt.rooms == [0, 1]
    assert flt.price_max == 60000

    # обновление того же фильтра
    flt2 = flt.model_copy(update={"price_max": 70000})
    await ps.upsert_filter(flt2, filter_id=fid)
    _, updated = (await ps.list_filters(42))[0]
    assert updated.price_max == 70000

    assert await ps.delete_filter(fid, user_id=42) is True
    assert await ps.list_filters(42) == []


async def test_due_filters_by_interval(ps: PersonalStore) -> None:
    fid = await ps.upsert_filter(UserFilter(user_id=1, interval_min=5))
    now = datetime(2026, 6, 21, 12, 0, 0)
    # Никогда не проверялся → должен быть due.
    assert any(i == fid for i, _ in await ps.due_filters(now))

    await ps.touch_filter(fid, now)
    # Сразу после проверки — не due.
    assert not any(i == fid for i, _ in await ps.due_filters(now + timedelta(minutes=1)))
    # Через интервал — снова due.
    assert any(i == fid for i, _ in await ps.due_filters(now + timedelta(minutes=6)))


# ── подписка / триал ───────────────────────────────────────────────────────
async def test_subscription_and_trial(ps: PersonalStore) -> None:
    now = datetime(2026, 6, 21, 12, 0, 0)
    await ps.get_or_create_subscriber(7, username="user7")
    assert await ps.is_active(7, now) is False
    assert await ps.free_sends_used(7) == 0

    await ps.increment_free_sends(7, 3)
    assert await ps.free_sends_used(7) == 3

    await ps.set_paid_until(7, now + timedelta(days=30))
    assert await ps.is_active(7, now) is True
    assert await ps.is_active(7, now + timedelta(days=31)) is False


async def test_pause_resume_within_cap(ps: PersonalStore) -> None:
    now = datetime(2026, 6, 21, 12, 0, 0)
    await ps.get_or_create_subscriber(8)
    await ps.set_paid_until(8, now + timedelta(days=30))
    paused = now + timedelta(days=10)
    assert await ps.pause(8, paused) is True
    assert await ps.is_active(8, paused) is False  # на паузе → не активна
    assert await ps.is_paused(8, paused) is True
    assert await ps.frozen_days_left(8) == 20

    # Возобновили через 1 день (в пределах лимита 2 дня) → +1 день заморозки.
    resumed = paused + timedelta(days=1)
    assert await ps.resume(8, resumed) is True
    assert await ps.is_active(8, resumed) is True
    assert await ps.paid_until_of(8) == now + timedelta(days=31)


async def test_pause_auto_resume_after_cap(ps: PersonalStore) -> None:
    now = datetime(2026, 6, 21, 12, 0, 0)
    await ps.get_or_create_subscriber(9)
    await ps.set_paid_until(9, now + timedelta(days=30))
    await ps.pause(9, now + timedelta(days=10))
    # Спустя >2 дней паузы is_active сам возобновляет (добавляет 2 дня заморозки).
    after = now + timedelta(days=13)
    assert await ps.is_active(9, after) is True
    assert await ps.is_paused(9, after) is False
    assert await ps.paid_until_of(9) == now + timedelta(days=32)


async def test_channel_bonus_once(ps: PersonalStore) -> None:
    now = datetime(2026, 6, 21, 12, 0, 0)
    await ps.get_or_create_subscriber(11)
    assert await ps.claim_channel_bonus(11, now, days=7) is True
    assert await ps.is_active(11, now) is True
    assert await ps.is_active(11, now + timedelta(days=8)) is False
    # Повторно — нельзя.
    assert await ps.claim_channel_bonus(11, now, days=7) is False


async def test_referral_credit_once(ps: PersonalStore) -> None:
    now = datetime(2026, 6, 21, 12, 0, 0)
    await ps.get_or_create_subscriber(100)  # реферер
    await ps.get_or_create_subscriber(200)  # приглашённый
    await ps.set_referred_by(200, 100)

    # Приглашённый оплатил → рефереру +7 дней (с нуля).
    ref = await ps.credit_referral(200, now, days=7)
    assert ref == 100
    assert await ps.is_active(100, now) is True
    assert await ps.is_active(100, now + timedelta(days=8)) is False

    # Повторно не начисляем.
    assert await ps.credit_referral(200, now, days=7) is None


async def test_referral_no_self_invite(ps: PersonalStore) -> None:
    await ps.get_or_create_subscriber(300)
    await ps.set_referred_by(300, 300)  # сам себя — игнор
    assert await ps.credit_referral(300, datetime(2026, 6, 21), days=7) is None


async def test_payment_resets_lifecycle_flags(ps: PersonalStore) -> None:
    now = datetime(2026, 6, 21, 12, 0, 0)
    await ps.get_or_create_subscriber(9)
    await ps.mark_nudged(9, "expiry")
    await ps.mark_nudged(9, "winback")
    # Новая оплата → флаги «истекает»/«вернись» сбрасываются (новый цикл).
    await ps.set_paid_until(9, now + timedelta(days=30))
    subs = {s.telegram_id: s for s in await ps.lifecycle_candidates()}
    assert subs[9].nudged_expiry is False
    assert subs[9].nudged_winback is False


# ── пер-юзер дедуп ─────────────────────────────────────────────────────────
async def test_per_user_sent_dedup(ps: PersonalStore) -> None:
    assert await ps.was_sent(1, "abc") is False
    await ps.mark_sent(1, "abc")
    assert await ps.was_sent(1, "abc") is True
    # другой пользователь — независимо
    assert await ps.was_sent(2, "abc") is False


# ── выборка свежих лотов с полными данными ──────────────────────────────────
async def test_recent_listings_roundtrip(storage: SqlStorage) -> None:
    listing = Listing(
        source=Source.YANDEX,
        external_id="9",
        url="https://realty.yandex.ru/offer/9",
        deal_type=DealType.LONG_RENT,
        property_type=PropertyType.FLAT,
        price=55000,
        area=38.0,
        rooms=1,
        city="Москва",
        district="Хамовники",
        address="улица Тест, 1",
        metro="Парк культуры",
        metro_distance_min=7,
        floor=5,
        floors_total=12,
        building_material="кирпич",
        photos=["https://img/a", "https://img/b"],
        collected_at=datetime(2026, 6, 21, 12, 0, 0),
    )
    await storage.upsert_listings([listing])
    got = await storage.recent_listings("Москва", datetime(2026, 6, 21, 0, 0, 0))
    assert len(got) == 1
    r = got[0]
    # полные данные восстановились (нужны для рендера в личке)
    assert r.photos == ["https://img/a", "https://img/b"]
    assert r.floor == 5 and r.building_material == "кирпич"
    assert r.address == "улица Тест, 1" and r.metro_distance_min == 7


# ── админ-статистика ─────────────────────────────────────────────────────────
async def test_admin_stats(ps: PersonalStore) -> None:
    now = datetime(2026, 7, 9, 12, 0, 0)
    await ps.get_or_create_subscriber(1)  # триал (не платил)
    await ps.get_or_create_subscriber(2)
    await ps.set_paid_until(2, now + timedelta(days=10))  # платный активный
    await ps.pause(2, now)  # его же на паузу (пауза только для активной подписки)
    await ps.get_or_create_subscriber(3)
    await ps.set_paid_until(3, now - timedelta(days=1))  # истёкший
    await ps.upsert_filter(UserFilter(user_id=1, name="ф", rooms=[1]))

    s = await ps.admin_stats(now)
    assert s["subscribers"] == 3
    assert s["paid"] == 1  # только #2 активен
    assert s["paused"] == 1  # #2
    assert s["trial"] == 1  # #1 (никогда не платил)
    assert s["expired"] == 1  # #3
    assert s["active_filters"] == 1
