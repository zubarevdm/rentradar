"""Тесты диспетчера персонального бота: матчинг по интервалу, дедуп, триал, скам."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from rentradar.config import Settings
from rentradar.models import DealType, Listing, PropertyType, ScoredListing, Source
from rentradar.personal import PersonalStore, UserFilter
from rentradar.personal.dispatcher import PersonalDispatcher
from rentradar.scoring import PlaceholderScoringEngine
from rentradar.storage import SqlStorage

NOW = datetime(2026, 6, 21, 12, 0, 0)


class FakePublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, ScoredListing]] = []

    async def publish(
        self, scored: ScoredListing, *, channel: str, use_custom_emoji: bool = False
    ) -> bool:
        self.sent.append((channel, scored))
        return True

    def render(self, scored: ScoredListing) -> str:
        return "post"


def _listing(
    ext: str,
    *,
    price: int = 55_000,
    photos: list[str] | None = None,
    collected: datetime | None = None,
) -> Listing:
    return Listing(
        source=Source.CIAN,
        external_id=ext,
        url=f"https://cian.ru/{ext}",
        deal_type=DealType.LONG_RENT,
        property_type=PropertyType.FLAT,
        price=price,
        area=38.0,
        rooms=1,
        city="Москва",
        district="Хамовники",
        address=f"улица Тестовая, {ext}",
        metro="Парк культуры",
        metro_distance_min=7,
        photos=["p.jpg"] if photos is None else photos,
        collected_at=collected or (NOW - timedelta(hours=1)),
    )


@pytest.fixture
async def env(tmp_path):
    st = SqlStorage(f"sqlite+aiosqlite:///{(tmp_path / 'd.db').as_posix()}")
    await st.init()
    ps = PersonalStore(st)
    yield st, ps
    await st.dispose()


def _dispatcher(st, ps, pub, **over):
    settings = Settings(
        free_sends_limit=over.get("free", 3),
        personal_cold_start_limit=over.get("cold_start", 12),
        personal_fresh_max=over.get("fresh_max", 15),
        personal_backlog_per_check=over.get("backlog", 5),
        personal_lookback_hours=24,
    )
    return PersonalDispatcher(
        store=ps, storage=st, scoring=PlaceholderScoringEngine(), publisher=pub, settings=settings
    )


async def test_sends_matches_and_dedups(env) -> None:
    st, ps = env
    await st.upsert_listings([_listing("1"), _listing("2")])
    await ps.get_or_create_subscriber(100)
    await ps.set_paid_until(100, NOW + timedelta(days=30))  # активна → без триал-лимита
    await ps.upsert_filter(UserFilter(user_id=100, rooms=[1], price_max=60_000, interval_min=5))
    pub = FakePublisher()
    disp = _dispatcher(st, ps, pub)

    n1 = await disp.run_due(NOW)
    assert n1 == 2
    assert all(ch == "100" for ch, _ in pub.sent)
    # Повторный прогон сразу — фильтр не due (интервал не прошёл) → 0.
    assert await disp.run_due(NOW + timedelta(minutes=1)) == 0
    # Через интервал — те же лоты уже отправлены → дедуп → 0.
    assert await disp.run_due(NOW + timedelta(minutes=6)) == 0


async def test_trial_gate_then_subscription(env) -> None:
    st, ps = env
    await st.upsert_listings([_listing(str(i)) for i in range(5)])
    await ps.get_or_create_subscriber(200)  # не подписан
    await ps.upsert_filter(UserFilter(user_id=200, rooms=[1], interval_min=5))
    pub = FakePublisher()
    disp = _dispatcher(st, ps, pub, free=3)

    # Триал: только 3 бесплатные отправки.
    assert await disp.run_due(NOW) == 3
    assert await ps.free_sends_used(200) == 3

    # Оформляем подписку → остальные 2 доезжают.
    await ps.set_paid_until(200, NOW + timedelta(days=30))
    assert await disp.run_due(NOW + timedelta(minutes=6)) == 2


async def test_scam_filtered_not_sent(env) -> None:
    st, ps = env
    # Лот без фото отсекается фрод-фильтром (PlaceholderScoringEngine).
    await st.upsert_listings([_listing("ok"), _listing("scam", photos=[])])
    await ps.get_or_create_subscriber(300)
    await ps.set_paid_until(300, NOW + timedelta(days=30))
    await ps.upsert_filter(UserFilter(user_id=300, rooms=[1], interval_min=5))
    pub = FakePublisher()
    disp = _dispatcher(st, ps, pub)

    n = await disp.run_due(NOW)
    assert n == 1  # отправлен только валидный
    assert pub.sent[0][1].listing.external_id == "ok"


async def test_respects_filter_criteria(env) -> None:
    st, ps = env
    await st.upsert_listings([_listing("cheap", price=40_000), _listing("exp", price=90_000)])
    await ps.get_or_create_subscriber(400)
    await ps.set_paid_until(400, NOW + timedelta(days=30))
    await ps.upsert_filter(UserFilter(user_id=400, price_max=60_000, interval_min=5))
    pub = FakePublisher()
    disp = _dispatcher(st, ps, pub)

    await disp.run_due(NOW)
    assert {s.listing.external_id for _, s in pub.sent} == {"cheap"}


async def test_cold_start_shows_burst_then_drains_backlog(env) -> None:
    # Новый фильтр + 6 лотов из базы: первый прогон показывает cold_start (3),
    # остальное досылается мягко бэклогом (по 2 за проверку).
    st, ps = env
    await st.upsert_listings([_listing(str(i)) for i in range(6)])
    await ps.get_or_create_subscriber(500)
    await ps.set_paid_until(500, NOW + timedelta(days=30))
    await ps.upsert_filter(UserFilter(user_id=500, rooms=[1], interval_min=5))
    pub = FakePublisher()
    disp = _dispatcher(st, ps, pub, cold_start=3, backlog=2)

    assert await disp.run_due(NOW) == 3  # холодный старт: свежайшие 3
    assert await disp.run_due(NOW + timedelta(minutes=6)) == 2  # бэклог по 2
    assert await disp.run_due(NOW + timedelta(minutes=12)) == 1  # остаток
    assert await disp.run_due(NOW + timedelta(minutes=18)) == 0  # всё разослано


async def test_fresh_not_blocked_by_backlog(env) -> None:
    # После холодного старта остаётся бэклог. Новый лот, появившийся «в моменте»,
    # должен уйти сразу, не встав в очередь за старым.
    st, ps = env
    await st.upsert_listings([_listing(f"old{i}") for i in range(6)])
    await ps.get_or_create_subscriber(600)
    await ps.set_paid_until(600, NOW + timedelta(days=30))
    await ps.upsert_filter(UserFilter(user_id=600, rooms=[1], interval_min=5))
    pub = FakePublisher()
    disp = _dispatcher(st, ps, pub, cold_start=2, backlog=1)

    assert await disp.run_due(NOW) == 2  # холодный старт
    pub.sent.clear()
    # Появился свежий лот (собран после последней проверки).
    await st.upsert_listings([_listing("hot", collected=NOW + timedelta(minutes=3))])
    sent = await disp.run_due(NOW + timedelta(minutes=6))
    ids = {s.listing.external_id for _, s in pub.sent}
    assert "hot" in ids  # свежий ушёл сразу, несмотря на бэклог
    assert sent >= 1
