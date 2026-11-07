import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from rentradar.collectors.avito import parse_detail
from rentradar.config import SearchProfile, Settings
from rentradar.models import ScoreBreakdown, ScoredListing, Source
from rentradar.personal.dispatcher import PersonalDispatcher
from rentradar.personal.filters import UserFilter
from rentradar.personal.store import PersonalStore
from rentradar.preparation import AvitoPreparer
from rentradar.publisher.telegram import TelegramPublisher
from rentradar.scoring import PlaceholderScoringEngine
from rentradar.storage import SqlStorage
from tests.test_personal_dispatcher import FakePublisher
from tests.test_publisher_send import FakeBot


@pytest.fixture
async def setup(tmp_path, sample_listing):
    storage = SqlStorage(f"sqlite+aiosqlite:///{(tmp_path / 'cards.db').as_posix()}")
    await storage.init()
    store = PersonalStore(storage)
    now = datetime.now(UTC).replace(tzinfo=None)
    await store.get_or_create_subscriber(100)
    await store.set_paid_until(100, now + timedelta(days=1))
    listing = sample_listing.model_copy(update={
        "source": Source.AVITO, "collected_at": now - timedelta(minutes=1),
        "published_at": None, "enriched_at": None, "prepared_at": None,
        "commission_pct": None, "deposit_rub": None, "address": "Тестовая 1",
    })
    settings = Settings(_env_file=None, avito_detail_delay_sec=0)
    yield storage, store, now, listing, settings
    await storage.dispose()


async def test_filter_before_duplicate_selection(setup):
    storage, store, now, listing, settings = setup
    await store.upsert_filter(UserFilter(user_id=100, interval_min=5, no_commission=True))
    await storage.upsert_listings([
        listing.model_copy(update={"commission_pct": 50}),
        listing.model_copy(update={"source": Source.CIAN, "commission_pct": 0}),
    ])
    publisher = FakePublisher()
    dispatcher = PersonalDispatcher(
        store=store, storage=storage, scoring=PlaceholderScoringEngine(),
        publisher=publisher, settings=settings,
    )
    assert await dispatcher.run_due(now) == 1
    assert publisher.sent[0][1].listing.source == Source.CIAN


async def test_late_preparation_is_delivered_in_new_only_mode(setup, monkeypatch):
    storage, store, now, listing, settings = setup
    await store.upsert_filter(UserFilter(
        user_id=100, interval_min=5, include_backlog=False,
    ))
    publisher = FakePublisher()
    dispatcher = PersonalDispatcher(
        store=store, storage=storage, scoring=PlaceholderScoringEngine(),
        publisher=publisher, settings=settings, require_prepared_avito=True,
    )
    assert await dispatcher.run_due(now) == 0
    listing = listing.model_copy(update={
        "collected_at": datetime.now(UTC).replace(tzinfo=None),
    })
    await storage.upsert_listings([listing])
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr("rentradar.preparation.fetch_detail", fetch)
    preparer = AvitoPreparer(storage, store, settings, Mock())
    await preparer.run()
    later = datetime.now(UTC).replace(tzinfo=None)
    assert await dispatcher.run_immediate(later) == 1
    assert await dispatcher.run_immediate(later) == 0
    await preparer.run()
    assert fetch.await_count == 1


async def test_preparing_old_card_does_not_revive_backlog(setup):
    storage, store, now, listing, settings = setup
    await store.upsert_filter(UserFilter(
        user_id=100, interval_min=5, include_backlog=False,
    ))
    publisher = FakePublisher()
    dispatcher = PersonalDispatcher(
        store=store, storage=storage, scoring=PlaceholderScoringEngine(),
        publisher=publisher, settings=settings, require_prepared_avito=True,
    )
    assert await dispatcher.run_due(now) == 0
    await storage.upsert_listings([listing])
    await storage.mark_prepared(listing, datetime.now(UTC).replace(tzinfo=None))
    assert await dispatcher.run_immediate(datetime.now(UTC).replace(tzinfo=None)) == 0


async def test_prepared_terms_are_filtered_before_send(setup, monkeypatch):
    storage, store, now, listing, settings = setup
    await store.upsert_filter(UserFilter(user_id=100, interval_min=5, no_commission=True))
    await storage.upsert_listings([listing])
    detail = parse_detail('<div data-marker="item-view/item-description">Комиссия 50%</div>')
    # Use explicit detail fields to isolate delivery ordering from HTML parsing.
    detail = detail._replace(commission_pct=50, commission_present=True)
    monkeypatch.setattr("rentradar.preparation.fetch_detail", AsyncMock(return_value=detail))
    await AvitoPreparer(storage, store, settings, Mock()).run()
    publisher = FakePublisher()
    dispatcher = PersonalDispatcher(
        store=store, storage=storage, scoring=PlaceholderScoringEngine(),
        publisher=publisher, settings=settings, require_prepared_avito=True,
    )
    assert await dispatcher.run_due(datetime.now(UTC).replace(tzinfo=None)) == 0
    assert publisher.sent == []


async def test_preparer_skips_handled_cards_and_reuses_partial_snapshot(setup, monkeypatch):
    storage, store, now, listing, settings = setup
    await store.upsert_filter(UserFilter(user_id=100, interval_min=5))
    other = listing.model_copy(update={"external_id": "other", "address": "Тестовая 2"})
    await storage.upsert_listings([listing, other])
    await store.mark_sent(100, listing.content_key, delivered=True, now=now)
    fetch = AsyncMock(return_value=parse_detail(
        '<div data-marker="item-view/item-description">Сдается квартира</div>',
    ))
    monkeypatch.setattr("rentradar.preparation.fetch_detail", fetch)
    preparer = AvitoPreparer(storage, store, settings, Mock())
    await preparer.run()
    saved = await storage.get_listing("avito", other.external_id)
    assert saved.prepared_at is not None and saved.enriched_at is None
    await preparer.run()
    assert fetch.await_count == 1


async def test_prepared_publisher_never_fetches_or_changes_matched_snapshot(setup, monkeypatch):
    storage, store, now, listing, settings = setup
    listing = listing.model_copy(update={"photos": [], "commission_pct": 0})
    await storage.upsert_listings([listing.model_copy(update={"commission_pct": 50})])
    fetch = AsyncMock(side_effect=AssertionError("network during delivery"))
    monkeypatch.setattr("rentradar.publisher.telegram.fetch_detail", fetch)
    bot = FakeBot()
    publisher = TelegramPublisher(
        dry_run=False, bot=bot, storage=storage, prepare_personal_separately=True,
    )
    assert await publisher.publish(
        ScoredListing(listing=listing, score=ScoreBreakdown(total=70)),
        channel="test", use_custom_emoji=True,
    )
    assert fetch.await_count == 0 and len(bot.message_calls) == 1


async def test_prepare_failure_respects_budget_but_releases_fallbacks(setup, monkeypatch):
    storage, store, now, listing, settings = setup
    await store.upsert_filter(UserFilter(user_id=100, interval_min=5))
    cards = [listing.model_copy(update={
        "external_id": str(i), "address": f"Тестовая {i}",
    }) for i in range(7)]
    await storage.upsert_listings(cards)
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr("rentradar.preparation.fetch_detail", fetch)
    await AvitoPreparer(storage, store, settings, Mock()).run()
    assert fetch.await_count == 1
    for card in cards:
        assert (await storage.get_listing("avito", card.external_id)).prepared_at is not None


async def test_serve_schedules_sources_and_cian_runs_while_avito_waits(tmp_path, monkeypatch):
    from rentradar import cli

    jobs = {}
    started = {source: asyncio.Event() for source in ("avito", "cian", "yandex")}
    hold_avito = asyncio.Event()
    calls = []

    class Scheduler:
        def add_job(self, function, trigger, **kwargs):
            jobs[kwargs.get("id", function.__name__)] = (function, kwargs)

        def start(self):
            pass

        def shutdown(self, **kwargs):
            pass

    async def collect(profile, *, source, on_new):
        calls.append(source)
        started[source].set()
        if source == "avito":
            await hold_avito.wait()
        return 0, {source: "ok"}

    storage = Mock(
        init=AsyncMock(), recompute_market_stats=AsyncMock(), dispose=AsyncMock(),
    )
    monkeypatch.setattr(cli, "SqlStorage", Mock(return_value=storage))
    monkeypatch.setattr(cli, "Pipeline", Mock(return_value=Mock(collect_only=collect)))
    monkeypatch.setattr(cli, "AsyncIOScheduler", Scheduler)
    monkeypatch.setattr(cli, "_build_collectors", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, "_load_profiles_or_warn", lambda settings: [
        SearchProfile(name="all", city="Москва"),
    ])
    settings = Settings(
        _env_file=None, dry_run=True, bot_token="", vision_enabled=False,
        db_path=tmp_path / "unused.db", collect_interval_min=2.5,
        avito_collect_interval_min=5, yandex_collect_interval_min=5,
        avito_browser_warmup=False,
    )
    task = asyncio.create_task(cli._serve(settings))
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in started.values())), 2)
        for source, minutes in (("cian", 2.5), ("avito", 5), ("yandex", 5)):
            function, kwargs = jobs[f"collect_{source}"]
            assert kwargs["minutes"] == minutes and kwargs["args"] == [source]
        function, kwargs = jobs["collect_cian"]
        await asyncio.wait_for(function(*kwargs["args"]), 2)
        assert calls.count("cian") == 2 and calls.count("avito") == 1
    finally:
        task.cancel()
        await task
