"""Cian .jpg URLs can return WebP; upload actual JPEG bytes to Telegram."""

from io import BytesIO
from pathlib import Path

import httpx
import pytest
from aiogram.types import BufferedInputFile
from PIL import Image

from rentradar.photos import telegram_photo
from rentradar.publisher import TelegramPublisher
from tests.test_publisher_send import FakeBot, _scored


def webp():
    output = BytesIO()
    Image.new("RGB", (800, 600), (25, 60, 100)).save(output, format="WEBP")
    return output.getvalue()


def test_webp_converted_without_resizing_and_jpeg_preserved():
    content, extension = telegram_photo(webp())
    with Image.open(BytesIO(content)) as photo:
        assert photo.format == "JPEG" and photo.size == (800, 600)
    assert extension == "jpg"
    original = (Path(__file__).parent / "fixtures/photo-large.jpg").read_bytes()
    assert telegram_photo(original) == (original, "jpg")


@pytest.mark.parametrize("payload", [b"<html>blocked</html>", b"RIFFbrokenWEBP"])
def test_non_image_is_not_uploaded(payload):
    with pytest.raises((OSError, ValueError)):
        telegram_photo(payload)


async def test_cian_cdn_webp_delivered_as_uploaded_album(monkeypatch):
    import rentradar.publisher.telegram as module

    real_client = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/blocked.jpg":
            return httpx.Response(200, text="<html>Access denied</html>")
        return httpx.Response(200, content=webp(), headers={"content-type": "image/webp"})

    def client(**kwargs):
        assert kwargs["proxy"] is None  # Never send Cian through the Avito-only proxy.
        return real_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(module.httpx, "AsyncClient", client)
    bot = FakeBot()
    original_send = bot.send_media_group
    async def reject_urls(**kwargs):
        if isinstance(kwargs["media"][0].media, str):
            from aiogram.exceptions import TelegramBadRequest
            from aiogram.methods import SendMediaGroup
            raise TelegramBadRequest(method=SendMediaGroup(chat_id="test", media=kwargs["media"]),
                                     message="invalid photo URL")
        return await original_send(**kwargs)
    bot.send_media_group = reject_urls
    async def reject_single_url(**kwargs):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import SendPhoto
        raise TelegramBadRequest(method=SendPhoto(chat_id="test", photo=kwargs["photo"]),
                                 message="invalid photo URL")
    bot.send_photo = reject_single_url
    pub = TelegramPublisher(dry_run=False, bot=bot, avito_proxy="http://avito-only.invalid")
    assert await pub.publish(
        _scored(photos=["https://images.cdn-cian.ru/a.jpg",
                        "https://images.cdn-cian.ru/b.jpg"]),
        channel="test", use_custom_emoji=True,
    )
    assert len(requests) == 2 and len(bot.album_calls) == 1
    media = bot.album_calls[0]["media"]
    assert len(media) == 2 and not bot.message_calls
    for item in media:
        assert isinstance(item.media, BufferedInputFile)
        with Image.open(BytesIO(item.media.data)) as picture:
            assert picture.format == "JPEG" and picture.size == (800, 600)
    assert not bot.markup_calls


async def test_cian_urls_are_sent_without_download(monkeypatch):
    pub = TelegramPublisher(dry_run=False, bot=FakeBot())
    async def forbidden(*args, **kwargs):
        raise AssertionError("Must not upload/download accessible Cian URLs")
    monkeypatch.setattr(pub, "_download_photos", forbidden)
    assert await pub.publish(_scored(photos=["https://images.cdn-cian.ru/a.jpg",
                                          "https://images.cdn-cian.ru/b.jpg"]), channel="test")
    assert len(pub._bot.album_calls) == 1


async def test_relay_falls_back_to_text_without_upload(monkeypatch):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendPhoto

    bot = FakeBot()

    async def reject(**kwargs):
        assert kwargs["request_timeout"] == 8
        raise TelegramBadRequest(
            method=SendPhoto(chat_id="test", photo="invalid"), message="invalid URL"
        )

    async def forbidden(*args, **kwargs):
        pytest.fail("Relay must not fall back to multipart uploads")

    monkeypatch.setattr(bot, "send_media_group", reject)
    monkeypatch.setattr(bot, "send_photo", reject)
    pub = TelegramPublisher(dry_run=False, bot=bot, via_relay=True)
    monkeypatch.setattr(pub, "_download_photos", forbidden)
    assert await pub.publish(_scored(photos=["https://img/a", "https://img/b"]), channel="test")
    assert len(bot.message_calls) == 1


async def test_cian_jpeg_proxy_sends_three_validated_urls():
    from urllib.parse import parse_qs, urlsplit

    bot = FakeBot()
    publisher = TelegramPublisher(
        dry_run=False, bot=bot, via_relay=True, cian_jpeg_proxy_enabled=True,
    )
    original = [f"https://images.cdn-cian.ru/{i}.jpg" for i in range(4)]
    assert await publisher.publish(_scored(photos=original), channel="test")
    media = bot.album_calls[0]["media"]
    assert len(media) == 3 and not bot.message_calls
    for photo, source in zip(media, original):
        parsed = urlsplit(photo.media)
        assert parsed.hostname == "wsrv.nl"
        query = parse_qs(parsed.query)
        assert query["url"] == [source] and query["output"] == ["jpg"]


def _avito_scored(photos: list[str]):
    from datetime import datetime

    from rentradar.models import Source

    scored = _scored(photos=photos)
    listing = scored.listing.model_copy(update={
        "source": Source.AVITO, "url": "https://www.avito.ru/moskva/kvartiry/x_1",
        "enriched_at": datetime(2026, 11, 6),  # без похода за карточкой
    })
    return scored.model_copy(update={"listing": listing})


async def test_avito_via_relay_sends_wsrv_urls_without_upload(monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    bot = FakeBot()
    pub = TelegramPublisher(dry_run=False, bot=bot, via_relay=True)

    async def forbidden(*args, **kwargs):
        pytest.fail("Relay must not upload Avito photos as multipart")

    monkeypatch.setattr(pub, "_download_photos", forbidden)
    original = [f"https://{i}0.img.avito.st/image/1/p{i}?cqp=2.x" for i in range(3)]
    assert await pub.publish(_avito_scored(original), channel="test")
    media = bot.album_calls[0]["media"]
    assert bot.album_calls[0]["request_timeout"] == 12
    assert [parse_qs(urlsplit(m.media).query)["url"][0] for m in media] == original
    assert all(urlsplit(m.media).hostname == "wsrv.nl" for m in media)


async def test_avito_via_relay_falls_back_to_text_quickly(monkeypatch):
    bot = FakeBot(fail={"album", "photo"})
    pub = TelegramPublisher(dry_run=False, bot=bot, via_relay=True)

    async def forbidden(*args, **kwargs):
        pytest.fail("Relay must not upload Avito photos as multipart")

    monkeypatch.setattr(pub, "_download_photos", forbidden)
    photos = ["https://10.img.avito.st/a", "https://20.img.avito.st/b", "https://evil.example/c"]
    assert await pub.publish(_avito_scored(photos), channel="test")
    assert len(bot.message_calls) == 1
