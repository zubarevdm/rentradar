"""Recover terms after temporary source failures and reuse them across recipients."""
import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from rentradar.collectors import avito
from rentradar.models import ScoreBreakdown, ScoredListing, Source
from rentradar.publisher import TelegramPublisher
from rentradar.storage import SqlStorage
from tests.test_publisher_send import FakeBot


@pytest.fixture
async def storage(tmp_path):
    db = SqlStorage(f"sqlite+aiosqlite:///{(tmp_path / 'recovery.db').as_posix()}")
    await db.init()
    yield db
    await db.dispose()


@pytest.mark.parametrize("status", [429, 439, 200])
async def test_source_block_stops_other_recipients_until_cooldown(monkeypatch, status):
    monkeypatch.setattr(avito, "_blocked_until", {})
    monkeypatch.setattr(avito, "_request_locks", {})
    monkeypatch.setattr(avito, "_block_cooldown_seconds", 720)
    clock = [1000.0]
    monkeypatch.setattr(avito.time, "monotonic", lambda: clock[0])
    request = Mock(return_value=SimpleNamespace(
        status_code=status, text="<title>Доступ ограничен: проверка безопасности</title>",
        headers={},
    ))
    monkeypatch.setattr(avito, "_blocking_get", request)
    assert await asyncio.gather(*(
        avito._avito_fetch_html(f"https://www.avito.ru/item_{i}") for i in range(5)
    )) == [None] * 5
    assert request.call_count == 1
    clock[0] += 719
    assert await avito._avito_fetch_html("https://www.avito.ru/search") is None
    assert request.call_count == 1
    request.return_value = SimpleNamespace(status_code=200, text="<html>listing</html>")
    clock[0] += 2
    assert await avito._avito_fetch_html("https://www.avito.ru/search") == "<html>listing</html>"
    assert request.call_count == 2


async def test_successful_send_enrichment_is_persisted_and_reused(
    storage, sample_listing, monkeypatch,
):
    html = (Path(__file__).parent / "fixtures/avito-8517144096-terms.html").read_text("utf-8")
    detail = avito.parse_detail(html, price=45000)
    listing = sample_listing.model_copy(update={
        "source": Source.AVITO, "external_id": "8517144096", "price": 45000,
        "deposit_rub": None, "commission_pct": None, "commission_rub": None,
        "enriched_at": None, "photos": [],
    })
    await storage.upsert_listings([listing])
    fetch = AsyncMock(return_value=detail)
    monkeypatch.setattr("rentradar.publisher.telegram.fetch_detail", fetch)
    bot = FakeBot()
    publisher = TelegramPublisher(dry_run=False, bot=bot, storage=storage)
    stale_candidate = ScoredListing(listing=listing, score=ScoreBreakdown(total=70))
    assert await publisher.publish(stale_candidate, channel="test")
    saved = await storage.get_listing("avito", listing.external_id)
    assert saved.move_in_total == 108000 and saved.commission_amount == 18000
    assert saved.enriched_at is not None
    # Another recipient holds the old in-memory candidate; use saved terms,
    # even if the source is currently blocked. No second network request.
    fetch.return_value = None
    assert await publisher.publish(stale_candidate, channel="other-test")
    assert fetch.await_count == 1
    assert all("При заезде: 108 000 ₽" in call["text"] for call in bot.message_calls)


async def test_failed_fetch_is_not_claimed_to_be_missing_in_ad(sample_listing, monkeypatch):
    listing = sample_listing.model_copy(update={
        "source": Source.AVITO, "price": 45000, "deposit_rub": None,
        "commission_pct": None, "commission_rub": None, "photos": [], "enriched_at": None,
    })
    monkeypatch.setattr("rentradar.publisher.telegram.fetch_detail", AsyncMock(return_value=None))
    bot = FakeBot()
    await TelegramPublisher(dry_run=False, bot=bot).publish(
        ScoredListing(listing=listing, score=ScoreBreakdown(total=70)), channel="test",
    )
    caption = bot.message_calls[0]["text"]
    # Not «не указаны»: the author may have stated them, we just could not read them.
    move_in = next(line for line in caption.splitlines() if "При заезде" in line)
    assert "залог и комиссия — в объявлении" in move_in
    assert "не указан" not in move_in and "<b>При заезде:" not in caption
    assert "Авито" not in caption  # no internal fetch details in the post


async def test_partial_detail_remains_eligible_for_enrichment(storage, sample_listing):
    listing = sample_listing.model_copy(update={
        "source": Source.AVITO, "deposit_rub": None, "commission_pct": None,
    })
    await storage.upsert_listings([listing])
    detail = avito.parse_detail('<div data-marker="item-view/item-description">Квартира</div>')
    await storage.save_enrichment("avito", listing.external_id,
                                  enriched_at=datetime(2026, 10, 26), **detail._asdict())
    assert (await storage.get_listing("avito", listing.external_id)).enriched_at is None
    assert await storage.unenriched_listings("avito", datetime(2020, 1, 1), 10)


async def test_search_fills_missing_terms_in_legacy_enriched_row(storage, sample_listing):
    listing = sample_listing.model_copy(update={
        "source": Source.AVITO, "deposit_rub": None, "commission_pct": None,
        "commission_rub": None, "enriched_at": datetime(2026, 10, 26),
    })
    await storage.upsert_listings([listing])
    fresh = listing.model_copy(update={"deposit_rub": 50000, "commission_pct": 0})
    assert await storage.upsert_listings([fresh]) == []
    saved = await storage.get_listing("avito", listing.external_id)
    assert saved.commission_amount == 0 and saved.move_in_total == listing.price + 50000
