"""Rental payments survive persistence and remain honest in Telegram captions."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from rentradar.collectors.avito import AvitoDetail, parse_detail
from rentradar.models import ScoreBreakdown, ScoredListing, Source
from rentradar.personal import PersonalStore, UserFilter
from rentradar.publisher import TelegramPublisher
from rentradar.storage import SqlStorage
from tests.test_publisher_send import FakeBot

NOW = datetime(2026, 10, 26, 12)
SINCE = datetime(2020, 1, 1)


@pytest.mark.parametrize("markup,expected", [
    ("<p>Описание квартиры без финансовых полей</p>", 680000),
    ("<p>Залог: 340000 ₽</p>", 680000),
    ("<p>Залог: 340000 ₽. Комиссия: уточняется</p>", None),
    ("<p>Залог: 340000 ₽. Комиссия: 50%</p>", 850000),
])
async def test_zero_commission_survives_partial_detail_in_storage_and_send(
    terms_storage, sample_listing, monkeypatch, markup, expected,
):
    listing = sample_listing.model_copy(update={
        "source": Source.AVITO, "price": 340000, "deposit_rub": 340000,
        "commission_pct": 0, "commission_rub": None, "photos": [], "enriched_at": None,
    })
    detail = parse_detail(f'<div data-marker="item-view/item-params">{markup}</div>',
                          price=340000)
    monkeypatch.setattr("rentradar.publisher.telegram.fetch_detail", AsyncMock(return_value=detail))
    bot = FakeBot()
    publisher = TelegramPublisher(bot=bot, dry_run=False)
    await publisher.publish(
        ScoredListing(listing=listing, score=ScoreBreakdown(total=70)), channel="test"
    )
    caption = bot.message_calls[0]["text"]
    if expected is None:
        assert "Полную стоимость заезда определить не удалось" in caption
    else:
        assert f"При заезде: {expected:,} ₽".replace(",", " ") in caption
    await terms_storage.upsert_listings([listing])
    await terms_storage.save_enrichment(
        "avito", listing.external_id, enriched_at=NOW, **detail._asdict()
    )
    saved = (await terms_storage.recent_listings("Москва", SINCE))[0]
    assert saved.move_in_total == expected
    if expected == 680000:
        assert saved.commission_amount == 0 and "без комиссии" in caption


async def test_user_zero_commission_listing_8410415010(terms_storage, sample_listing):
    from pathlib import Path

    html = (Path(__file__).parent / "fixtures/avito-zero-commission-8410415010.html").read_text(
        encoding="utf-8"
    )
    detail = parse_detail(html, price=340000)
    assert detail.deposit_rub == 340000 and detail.commission_pct == 0
    listing = sample_listing.model_copy(update={
        "source": Source.AVITO, "external_id": "8410415010", "price": 340000,
    })
    await terms_storage.upsert_listings([listing])
    await terms_storage.save_enrichment(
        "avito", listing.external_id, enriched_at=NOW, **detail._asdict()
    )
    saved = (await terms_storage.recent_listings("Москва", SINCE))[0]
    caption = TelegramPublisher(dry_run=True).render(
        ScoredListing(listing=saved, score=ScoreBreakdown(total=70))
    )
    assert saved.move_in_total == 680000
    assert "При заезде: 680 000 ₽" in caption and "без комиссии (0%)" in caption


@pytest.fixture
async def terms_storage(tmp_path):
    storage = SqlStorage(f"sqlite+aiosqlite:///{(tmp_path / 'payments.db').as_posix()}")
    await storage.init()
    try:
        yield storage
    finally:
        await storage.dispose()


@pytest.mark.parametrize("personal", [False, True])
@pytest.mark.parametrize("price,terms,total,deposit,commission", [
    (50000, "Залог: 50 000 ₽. Комиссия: 50%", 125000, 50000, 25000),
    (50000, "Залог: 50 000 ₽. Комиссия: 25 001 руб.", 125001, 50000, 25001),
    (100000, "Залог: 100000 ₽. Комиссия: 0 %", 200000, 100000, 0),
    (50000, "Без залога, без комиссии", 50000, 0, 0),
    (50000, "Залог: 50 000 ₽", None, 50000, None),
])
async def test_parser_storage_caption(
    terms_storage, sample_listing, personal, price, terms, total, deposit, commission
):
    listing = sample_listing.model_copy(update={"source": Source.AVITO, "price": price})
    await terms_storage.upsert_listings([listing])
    detail = parse_detail(
        f"<html><body><h2>Условия аренды</h2><p>{terms}</p>"
        "<h2>Правила</h2></body></html>", price=price
    )
    await terms_storage.save_enrichment(
        "avito", listing.external_id, enriched_at=NOW, **detail._asdict()
    )
    saved = (await terms_storage.recent_listings("Москва", SINCE))[0]
    assert saved.deposit_rub == deposit
    assert saved.commission_amount == commission
    assert saved.move_in_total == total
    caption = TelegramPublisher(dry_run=True).render(
        ScoredListing(listing=saved, score=ScoreBreakdown(total=70)),
        use_custom_emoji=personal,
    )
    assert f"первый месяц {price:,}".replace(",", " ") in caption
    if total is None:
        assert "Полную стоимость заезда определить не удалось" in caption
        assert "Не указаны: комиссия" in caption
        assert "<b>При заезде:" not in caption
    else:
        assert f"При заезде: {total:,} ₽".replace(",", " ") in caption


async def test_new_payment_representations_replace_stale_values(terms_storage, sample_listing):
    listing = sample_listing.model_copy(update={"source": Source.AVITO, "price": 50000})
    await terms_storage.upsert_listings([listing])

    async def save(**payments):
        snapshot = {"deposit_rub": 50000, "commission_pct": 0, **payments}
        await terms_storage.save_enrichment(
            "avito", listing.external_id, enriched_at=NOW, **snapshot
        )
        return (await terms_storage.recent_listings("Москва", SINCE))[0]

    fixed = await save(deposit_rub=50000, commission_rub=25001)
    assert fixed.move_in_total == 125001
    percentage = await save(commission_pct=50)
    assert percentage.commission_rub is None and percentage.move_in_total == 125000
    zero = await save(commission_rub=0)
    assert zero.commission_pct is None and zero.move_in_total == 100000
    percentage_zero = await save(commission_pct=0)
    assert percentage_zero.commission_rub is None and percentage_zero.move_in_total == 100000
    split = await save(deposit_due_rub=10000, deposit_installments=True)
    assert split.move_in_total == 60000
    unknown_split = await save(deposit_rub=50000, deposit_installments=True)
    assert unknown_split.deposit_due is None and unknown_split.move_in_total is None
    full = await save(deposit_rub=50000, deposit_installments=False)
    assert full.deposit_due == 50000 and full.move_in_total == 100000
    unknown = await save(deposit_rub=None, commission_pct=None)
    assert unknown.deposit_rub is None and unknown.commission_amount is None
    assert unknown.move_in_total is None


async def test_price_change_invalidates_monthly_deposit_and_requeues_detail(
    terms_storage, sample_listing
):
    initial = sample_listing.model_copy(update={
        "source": Source.AVITO, "price": 50000, "collected_at": NOW,
    })
    await terms_storage.upsert_listings([initial])
    await terms_storage.save_enrichment(
        "avito", initial.external_id, enriched_at=NOW,
        deposit_rub=50000, commission_pct=50,
    )
    updated = initial.model_copy(update={"price": 60000})
    assert await terms_storage.upsert_listings([updated]) == []
    pending = (await terms_storage.recent_listings("Москва", SINCE))[0]
    assert pending.price == 60000 and pending.fingerprint == updated.fingerprint
    assert pending.enriched_at is None and pending.deposit_rub is None
    assert pending.move_in_total is None
    assert len(await terms_storage.unenriched_listings("avito", SINCE, 1)) == 1

    detail = parse_detail(
        "<h2>Условия аренды</h2>Залог: 1 месяц. Комиссия: 50%<h2>Правила</h2>",
        price=pending.price,
    )
    await terms_storage.save_enrichment(
        "avito", initial.external_id, enriched_at=NOW, **detail._asdict()
    )
    saved = (await terms_storage.recent_listings("Москва", SINCE))[0]
    assert saved.deposit_rub == 60000 and saved.commission_amount == 30000
    assert saved.move_in_total == 150000


def test_unknown_installment_caption_preserves_full_deposit(sample_listing):
    listing = sample_listing.model_copy(update={
        "price": 50000, "deposit_rub": 100000, "deposit_installments": True,
        "commission_pct": 0,
    })
    caption = TelegramPublisher(dry_run=True).render(
        ScoredListing(listing=listing, score=ScoreBreakdown(total=70))
    )
    assert "залог всего 100 000 ₽ (платёж сейчас неизвестен)" in caption
    assert "Полную стоимость заезда определить не удалось" in caption
    assert "<b>При заезде:" not in caption


async def test_legacy_terms_and_metro_migrations_are_once_only(terms_storage, sample_listing):
    personal = PersonalStore(terms_storage)
    await personal.get_or_create_subscriber(77)
    filter_id = await personal.upsert_filter(UserFilter(
        user_id=77, max_metro_min=5, price_max=90000,
    ))
    await personal.mark_sent(77, "migration-test-key")
    listing = sample_listing.model_copy(update={
        "source": Source.AVITO, "enriched_at": NOW,
        "deposit_rub": 2, "commission_pct": 0, "metro_distance_min": 5,
    })
    await terms_storage.upsert_listings([listing])
    async with terms_storage._engine.begin() as conn:
        await conn.execute(text(
            "DELETE FROM data_versions WHERE name IN ('rental_terms_v3', 'walking_metro_v1')"
        ))
        await conn.execute(text("INSERT INTO data_versions(name) VALUES ('rental_terms_v2')"))
    await terms_storage.init()
    saved = (await terms_storage.recent_listings("Москва", SINCE))[0]
    assert saved.enriched_at is None and saved.metro_distance_min is None
    assert saved.deposit_rub is None and saved.commission_amount is None
    _, unchanged_filter = await personal.get_filter(filter_id)
    assert unchanged_filter.max_metro_min == 5 and unchanged_filter.price_max == 90000
    assert await personal.was_sent(77, "migration-test-key")
    assert await terms_storage.unenriched_listings("avito", SINCE, 1)
    await terms_storage.save_enrichment(
        "avito", listing.external_id, enriched_at=NOW, deposit_rub=100000, commission_pct=0
    )
    await terms_storage.upsert_listings([listing.model_copy(update={"metro_distance_min": 4})])
    await terms_storage.init()
    saved = (await terms_storage.recent_listings("Москва", SINCE))[0]
    assert saved.enriched_at == NOW and saved.deposit_rub == 100000
    assert saved.metro_distance_min == 4
    assert await personal.get_filter(filter_id)
    assert await personal.was_sent(77, "migration-test-key")


@pytest.mark.parametrize("source", [Source.AVITO, Source.CIAN, Source.YANDEX])
async def test_new_collection_replaces_stale_metro_time(terms_storage, sample_listing, source):
    listing = sample_listing.model_copy(update={"source": source, "metro_distance_min": 5})
    await terms_storage.upsert_listings([listing])
    assert not await terms_storage.upsert_listings([
        listing.model_copy(update={"metro_distance_min": None, "metro": "Новая станция"})
    ])
    saved = (await terms_storage.recent_listings("Москва", SINCE))[0]
    assert saved.metro == "Новая станция" and saved.metro_distance_min is None


async def test_publish_enriches_terms_even_without_photos(sample_listing, monkeypatch):
    bot = FakeBot()
    fetch = AsyncMock(return_value=AvitoDetail([], 50000, 50, None, None))
    monkeypatch.setattr("rentradar.publisher.telegram.fetch_detail", fetch)
    listing = sample_listing.model_copy(update={
        "source": Source.AVITO, "price": 50000, "photos": [], "commission_rub": 1,
    })
    publisher = TelegramPublisher(bot=bot, dry_run=False)
    assert await publisher.publish(
        ScoredListing(listing=listing, score=ScoreBreakdown(total=70)), channel="test"
    )
    fetch.assert_awaited_once()
    assert "При заезде: 125 000 ₽" in bot.message_calls[0]["text"]
