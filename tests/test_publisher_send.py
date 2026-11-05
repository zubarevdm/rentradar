"""Тесты отправки в Telegram на фейковом боте (без сети).

Проверяем выбор способа (альбом/фото/текст), каскад фолбэков при битых фото,
обработку флуд-лимита и то, что неуспех честно возвращает False.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from rentradar.models import (
    DealType,
    Listing,
    PropertyType,
    ScoreBreakdown,
    ScoredListing,
    SignalScore,
    Source,
)
from rentradar.publisher import TelegramPublisher


@pytest.fixture(autouse=True)
def offline_photos(monkeypatch):
    # Delivery tests exercise Bot API fallbacks; CDN/image conversion has its
    # own transport-level tests in test_cian_photo_delivery.py.
    from aiogram.types import BufferedInputFile

    async def download(self, urls, **kwargs):
        return [BufferedInputFile(b"jpeg", filename=f"{i}.jpg") for i, _ in enumerate(urls)]

    monkeypatch.setattr(TelegramPublisher, "_download_photos", download)


def _bad_request(msg: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=object(), message=msg)  # type: ignore[arg-type]


def _retry_after(sec: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=object(), message="flood", retry_after=sec)  # type: ignore[arg-type]


class FakeBot:
    """Записывает вызовы; режимы отказа задаются множеством `fail`."""

    def __init__(self, fail: set[str] | None = None, retry_once: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.retry_once = retry_once or set()
        self._retried: set[str] = set()
        self.photo_calls: list[dict] = []
        self.message_calls: list[dict] = []
        self.album_calls: list[dict] = []
        self.markup_calls: list[dict] = []

    async def _maybe_fail(self, kind: str) -> None:
        if kind in self.retry_once and kind not in self._retried:
            self._retried.add(kind)
            raise _retry_after(0)
        if kind in self.fail:
            raise _bad_request(f"{kind} rejected")

    async def send_photo(self, **kwargs: object) -> None:
        await self._maybe_fail("photo")
        self.photo_calls.append(kwargs)

    async def send_message(self, **kwargs: object) -> None:
        await self._maybe_fail("message")
        self.message_calls.append(kwargs)

    async def send_media_group(self, **kwargs: object) -> list[SimpleNamespace]:
        await self._maybe_fail("album")
        self.album_calls.append(kwargs)
        return [SimpleNamespace(message_id=i + 100) for i, _ in enumerate(kwargs["media"])]

    async def edit_message_reply_markup(self, **kwargs: object) -> None:
        await self._maybe_fail("markup")
        self.markup_calls.append(kwargs)


def _scored(*, photos: list[str]) -> ScoredListing:
    listing = Listing(
        source=Source.CIAN,
        external_id="1",
        url="https://cian.ru/1",
        deal_type=DealType.LONG_RENT,
        property_type=PropertyType.FLAT,
        price=55_000,
        area=38.0,
        rooms=1,
        city="Москва",
        district="Хамовники",
        metro="Парк культуры",
        metro_distance_min=7,
        photos=photos,
        collected_at=datetime(2026, 6, 21, 12, 0, 0),
    )
    score = ScoreBreakdown(
        total=80.0,
        signals=[SignalScore(name="below_market", value=0.9, weight=0.45, detail="−20% к рынку")],
    )
    return ScoredListing(listing=listing, score=score)


async def test_publish_with_photo_uses_send_photo() -> None:
    bot = FakeBot()
    pub = TelegramPublisher(dry_run=False, bot=bot)  # type: ignore[arg-type]
    ok = await pub.publish(_scored(photos=["https://img/1.jpg"]), channel="@ch")

    assert ok is True
    assert len(bot.photo_calls) == 1
    assert not bot.message_calls and not bot.album_calls


async def test_publish_with_multiple_photos_uses_album() -> None:
    bot = FakeBot()
    pub = TelegramPublisher(dry_run=False, bot=bot)  # type: ignore[arg-type]
    ok = await pub.publish(_scored(photos=["a.jpg", "b.jpg", "c.jpg"]), channel="@ch")

    assert ok is True
    assert len(bot.album_calls) == 1
    media = bot.album_calls[0]["media"]
    assert len(media) == 2  # Cian relay-safe album


async def test_publish_without_photo_uses_send_message() -> None:
    bot = FakeBot()
    pub = TelegramPublisher(dry_run=False, bot=bot)  # type: ignore[arg-type]
    ok = await pub.publish(_scored(photos=[]), channel="@ch")

    assert ok is True
    assert len(bot.message_calls) == 1


async def test_bad_album_falls_back_to_text() -> None:
    # Альбом и одиночное фото отклонены (битые ссылки) → уходит текстом.
    bot = FakeBot(fail={"album", "photo"})
    pub = TelegramPublisher(dry_run=False, bot=bot)  # type: ignore[arg-type]
    ok = await pub.publish(_scored(photos=["a.jpg", "b.jpg"]), channel="@ch")

    assert ok is True
    assert len(bot.message_calls) == 1
    assert not bot.album_calls and not bot.photo_calls


async def test_returns_false_when_everything_fails() -> None:
    bot = FakeBot(fail={"album", "photo", "message"})
    pub = TelegramPublisher(dry_run=False, bot=bot)  # type: ignore[arg-type]
    ok = await pub.publish(_scored(photos=["a.jpg", "b.jpg"]), channel="@ch")
    assert ok is False


async def test_flood_retry_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    # Первый вызов альбома → RetryAfter, после ожидания повтор успешен.
    import rentradar.publisher.telegram as tg

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(tg.asyncio, "sleep", no_sleep)
    bot = FakeBot(retry_once={"album"})
    pub = TelegramPublisher(dry_run=False, bot=bot)  # type: ignore[arg-type]
    ok = await pub.publish(_scored(photos=["a.jpg", "b.jpg"]), channel="@ch")

    assert ok is True
    assert len(bot.album_calls) == 1  # повтор прошёл


async def test_dry_run_returns_true_without_calls() -> None:
    bot = FakeBot()
    pub = TelegramPublisher(dry_run=True, bot=bot)  # type: ignore[arg-type]
    ok = await pub.publish(_scored(photos=["https://img/1.jpg"]), channel="@ch")
    assert ok is True
    assert not bot.photo_calls and not bot.message_calls and not bot.album_calls


@pytest.mark.parametrize("photos", [[], ["a.jpg"], ["a.jpg", "b.jpg"]])
async def test_personal_listing_keeps_source_link_without_buttons(photos):
    bot = FakeBot()
    pub = TelegramPublisher(dry_run=False, bot=bot)
    assert await pub.publish(_scored(photos=photos), channel="42", use_custom_emoji=True)
    assert not bot.markup_calls
    if len(photos) == 2:
        call = bot.album_calls[0]
        text = call["media"][0].caption
    elif photos:
        call = bot.photo_calls[0]
        text = call["caption"]
    else:
        call = bot.message_calls[0]
        text = call["text"]
    assert not call.get("reply_markup")
    assert '<a href="https://cian.ru/1">' in text


@pytest.mark.parametrize(
    "file_name,notice", [("photo-preview.jpg", True), ("photo-large.jpg", False)]
)
async def test_original_photo_bytes_are_not_resized_or_recompressed(file_name, notice, monkeypatch):
    from pathlib import Path
    from unittest.mock import AsyncMock

    from aiogram.types import BufferedInputFile

    data = (Path(__file__).parent / "fixtures" / file_name).read_bytes()
    uploaded = BufferedInputFile(data, filename="photo.jpg")
    bot = FakeBot()
    pub = TelegramPublisher(dry_run=False, bot=bot)
    monkeypatch.setattr(pub, "_download_photos", AsyncMock(return_value=[uploaded]))
    scored = _scored(photos=["https://img/original.jpg"])
    scored = scored.model_copy(
        update={
            "listing": scored.listing.model_copy(
                update={
                    "source": Source.AVITO,
                    "enriched_at": datetime(2026, 10, 25),
                }
            )
        }
    )
    assert await pub.publish(scored, channel="42", use_custom_emoji=True)
    sent = bot.photo_calls[0]
    assert sent["photo"].data == data
    assert ("Полные фото — в объявлении" in sent["caption"]) is notice
    assert "https://cian.ru/1" in sent["caption"]
