from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from rentradar.avito_enrichment import enrich_avito
from rentradar.collectors.avito import parse_detail
from rentradar.config import Settings
from rentradar.errors import DetailDeferred
from rentradar.models import Source
from rentradar.storage import SqlStorage
from rentradar.worker import utcnow


@pytest.fixture
async def queue_store(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'queue.db').as_posix()}"
    storage = SqlStorage(url)
    await storage.init()
    yield storage, url
    await storage.dispose()


def _listing(sample_listing, external_id="card"):
    return sample_listing.model_copy(update={
        "source": Source.AVITO, "external_id": external_id, "collected_at": utcnow(),
        "deposit_rub": None, "commission_rub": None, "commission_pct": None,
    })


async def test_detail_lease_recovers_after_reopen(queue_store, sample_listing):
    storage, url = queue_store
    listing = _listing(sample_listing)
    await storage.upsert_changes([listing])
    now = utcnow()
    first, _ = await storage.claim_detail(listing, now, 10)
    restarted = SqlStorage(url)
    try:
        assert await restarted.ready_details(now, 1) == []
        later = now + timedelta(seconds=11)
        assert [item.external_id for item in await restarted.ready_details(later, 1)] == ["card"]
        second, _ = await restarted.claim_detail(listing, later, 10)
        assert first.revision == second.revision and first.until != second.until
        assert await restarted.save_enrichment("avito", "card", enriched_at=later,
                                               claim=first, commission_rub=0) is None
        saved = await restarted.save_enrichment("avito", "card", enriched_at=later,
                                                claim=second, commission_rub=0)
        assert saved is not None and saved.commission_amount == 0
        assert await restarted.detail_backlog(later) == (0, 0)
    finally:
        await restarted.dispose()


async def test_same_price_search_update_rejects_old_enrichment(queue_store, sample_listing):
    storage, _ = queue_store
    listing = _listing(sample_listing)
    await storage.upsert_changes([listing])
    claim, _ = await storage.claim_detail(listing, utcnow(), 10)
    latest = listing.model_copy(update={"description": "Новые условия", "collected_at": utcnow()})
    await storage.upsert_changes([latest])
    assert await storage.save_enrichment("avito", "card", enriched_at=utcnow(), claim=claim,
                                         description="Устаревшее описание") is None
    saved = await storage.get_listing("avito", "card")
    assert saved is not None and saved.description == "Новые условия"
    replacement, _ = await storage.claim_detail(latest, utcnow(), 10)
    assert replacement.revision != claim.revision


async def test_broken_card_does_not_block_next_detail(queue_store, sample_listing, monkeypatch):
    storage, _ = queue_store
    await storage.upsert_changes([_listing(sample_listing, "good"),
                                  _listing(sample_listing, "slow")])
    html = '<div data-marker="item-specific-params">Залог 50 000 ₽ · Без комиссии</div>'
    fetch = AsyncMock(side_effect=[DetailDeferred("TimeoutError"), parse_detail(html)])
    monkeypatch.setattr("rentradar.avito_enrichment.fetch_detail", fetch)
    settings = Settings(_env_file=None, avito_detail_max_per_run=2, avito_detail_delay_sec=0)
    assert await enrich_avito(settings, storage) == 1
    assert fetch.await_count == 2
    assert await storage.ready_details(utcnow(), 2) == []
    assert (await storage.detail_backlog(utcnow()))[0] == 1
