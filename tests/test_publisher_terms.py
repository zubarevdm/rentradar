"""Money uncertainty stays in both channel and personal photo captions."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from rentradar.models import ScoreBreakdown, ScoredListing, Source
from rentradar.publisher import TelegramPublisher
from rentradar.publisher.telegram import _telegram_length
from tests.test_publisher_send import FakeBot


@pytest.mark.parametrize("personal", [False, True])
@pytest.mark.parametrize(
    "deposit,commission,expected",
    [
        (50000, 50, "При заезде: 125 000 ₽"),
        (None, 50, "от 75 000 ₽ — первый месяц + комиссия 25 000 ₽, залог не указан"),
        (50000, None, "от 100 000 ₽ — первый месяц + залог 50 000 ₽, комиссия не указана"),
        (None, None, "При заезде: от 50 000 ₽, залог и комиссия не указаны"),
        (0, 0, "без залога + без комиссии"),
        (0, None, "от 50 000 ₽, без залога, комиссия не указана"),
        (None, 0, "от 50 000 ₽, без комиссии, залог не указан"),
    ],
)
def test_move_in_unknowns(sample_listing, personal, deposit, commission, expected):
    listing = sample_listing.model_copy(
        update={"price": 50000, "deposit_rub": deposit, "commission_pct": commission}
    )
    text = TelegramPublisher(dry_run=True).render(
        ScoredListing(listing=listing, score=ScoreBreakdown(total=70, signals=[])),
        use_custom_emoji=personal,
    )
    assert expected in text
    if deposit is None or commission is None:
        assert listing.move_in_total is None
        assert "<b>При заезде:" not in text
    else:
        assert listing.move_in_total == 50000 + deposit + round(50000 * commission / 100)


@pytest.mark.parametrize(
    "utilities,meters,expected",
    [
        (None, None, "/ мес. ЖКУ и счётчики не указаны"),
        (5500, False, "+ 5 500 ₽ ЖКУ, счётчики отдельно"),
        (0, True, "ЖКУ и счётчики включены"),
        (None, True, "ЖКУ не указаны, счётчики включены"),
        (5500, None, "+ 5 500 ₽ ЖКУ, счётчики не указаны"),
    ],
)
def test_utility_states(sample_listing, utilities, meters, expected):
    listing = sample_listing.model_copy(
        update={
            "utilities_rub": utilities,
            "meters_included": meters,
            "deposit_rub": 50000,
            "commission_pct": 50,
        }
    )
    text = TelegramPublisher(dry_run=True).render(
        ScoredListing(listing=listing, score=ScoreBreakdown(total=70, signals=[]))
    )
    assert expected in text
    move = next(line for line in text.splitlines() if "При заезде" in line)
    assert "ЖКУ" not in move and "счётчики" not in move


class ValidHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "a", "tg-emoji"}
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag


@pytest.mark.parametrize("personal", [False, True])
@pytest.mark.parametrize(
    "deposit,commission", [(None, None), (50000, None), (None, 50), (50000, 50)]
)
async def test_long_html_keeps_photos_money_and_source(
    sample_listing, personal, deposit, commission, monkeypatch
):
    from unittest.mock import AsyncMock

    from aiogram.types import BufferedInputFile

    listing = sample_listing.model_copy(
        update={
            "source": Source.CIAN,
            "metro": '<script> & "' * 100,
            "district": "Район" * 250,
            "deposit_rub": deposit,
            "commission_pct": commission,
            "url": "https://example.test/listing?x=1&y=2",
            "photos": ["p1", "p2"],
        }
    )
    bot = FakeBot()
    publisher = TelegramPublisher(
        dry_run=False,
        bot=bot,
        cta_text="CTA" * 600,
        cta_url="https://example.test",
        cta_public_text="CTA" * 600,
        cta_public_url="https://example.test",
        emoji={"home": "111", "price": "222"},
    )
    scored = ScoredListing(listing=listing, score=ScoreBreakdown(total=70, signals=[]))
    monkeypatch.setattr(publisher, "_download_photos", AsyncMock(return_value=[
        BufferedInputFile(b"photo1", filename="1.jpg"),
        BufferedInputFile(b"photo2", filename="2.jpg"),
    ]))
    assert await publisher.publish(scored, channel="test", use_custom_emoji=personal)
    assert len(bot.album_calls) == 1 and not bot.message_calls
    media = bot.album_calls[0]["media"]
    assert len(media) == 2
    caption = media[0].caption
    assert _telegram_length(caption) <= 1024
    assert "https://example.test/listing?x=1&amp;y=2" in caption
    assert "🔑" in caption and "ЖКУ" in caption
    assert "не указан" in caption
    parser = ValidHTML()
    parser.feed(caption)
    assert parser.stack == []
