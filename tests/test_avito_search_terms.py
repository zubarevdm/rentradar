"""Terms visible on real search cards must survive normalize/upsert/render."""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from rentradar.collectors.avito import AvitoCollector, _parse_cards, _wrap_card, parse_detail
from rentradar.models import ScoreBreakdown, ScoredListing
from rentradar.publisher import TelegramPublisher
from rentradar.storage import SqlStorage
from tests.test_publisher_send import FakeBot


def listing(terms, price=160000, description=""):
    # The item-specific-params contents were observed on live card 4625696471
    # on 2026-10-26; the wrapper is reduced to parser-relevant elements.
    html = f'''<div data-marker="item" data-item-id="4625696471">
      <a data-marker="item-title" href="/moskva/kvartiry/x_4625696471">
      2-к. квартира, 65 м², 7/23 эт.</a><meta itemprop="price" content="{price}">
      <div data-marker="item-address"><a data-marker="street_link">Тестовая ул.</a>,
      <a data-marker="house_link">1</a></div>
      <p data-marker="item-specific-params">{terms}</p>
      <p style="--module-max-lines-size:4">{description}</p></div>'''
    cards = _parse_cards(html)
    assert cards and len(cards) == 1
    result = AvitoCollector().normalize(_wrap_card(cards[0]))
    assert result is not None
    return result


@pytest.mark.parametrize("terms,total", [
    ("Залог&nbsp;160&nbsp;000&nbsp;₽ · Без&nbsp;комиссии · "
     "ЖКУ&nbsp;включены&nbsp;без&nbsp;счетчиков",
     320000),
    ("Залог 1 месяц · Комиссия 50 %", 400000),
    ("Залог 2 месяца · Комиссия 20 001 ₽", 500001),
    ("Без залога · Без комиссии", 160000),
    ("Залог 160 000 ₽ · Комиссия уточняется", None),
])
def test_search_card_conditions(terms, total):
    assert listing(terms).move_in_total == total


def test_live_screenshot_dom_and_recurring_utilities():
    html = (Path(__file__).parent / "fixtures/avito-rental-dom-20261026.html").read_text("utf-8")
    detail = parse_detail(html, price=100000)
    assert detail.deposit_rub == 100000 and detail.commission_pct == 0
    assert detail.meters_included is False and detail.utilities_rub == 0
    card = listing("Залог 160000 ₽ · Без комиссии · ЖКУ включены без счетчиков")
    assert card.move_in_total == 320000 and card.meters_included is False
    assert card.utilities_included is True


def test_search_description_changes_initial_payment():
    card = listing("Залог 160000 ₽ · Без комиссии",
                   description="Оплата за первый и последний месяц.")
    assert card.move_in_total == 480000


@pytest.mark.parametrize("description,due,total", [
    ("Залог можно разбить на 2 месяца.", None, None),
    ("Залог частями, при заезде 40000 ₽.", 40000, 200000),
])
def test_summary_deposit_with_schedule_in_description(description, due, total):
    card = listing("Залог 160000 ₽ · Без комиссии", description=description)
    assert card.deposit_installments is True
    assert card.deposit_due == due and card.move_in_total == total


async def test_same_price_recollection_then_blocked_detail_keeps_total(tmp_path, monkeypatch):
    storage = SqlStorage(f"sqlite+aiosqlite:///{(tmp_path / 'search.db').as_posix()}")
    await storage.init()
    try:
        old = listing("")
        await storage.upsert_listings([old])
        fresh = listing("Залог 160000 ₽ · Без комиссии")
        assert await storage.upsert_listings([fresh]) == []  # No duplicate delivery.
        saved = (await storage.recent_listings("Москва", datetime(2020, 1, 1)))[0]
        assert saved.move_in_total == 320000
        monkeypatch.setattr(
            "rentradar.publisher.telegram.fetch_detail", AsyncMock(return_value=None)
        )
        bot = FakeBot()
        pub = TelegramPublisher(dry_run=False, bot=bot)
        assert await pub.publish(ScoredListing(listing=saved, score=ScoreBreakdown(total=70)),
                                 channel="test", use_custom_emoji=True)
        assert "При заезде: 320 000 ₽" in bot.message_calls[0]["text"]
    finally:
        await storage.dispose()
