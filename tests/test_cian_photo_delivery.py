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
    pub = TelegramPublisher(dry_run=False, bot=bot, avito_proxy="http://avito-only.invalid")
    assert await pub.publish(
        _scored(photos=["https://images.cdn-cian.ru/a.jpg",
                        "https://images.cdn-cian.ru/blocked.jpg",
                        "https://images.cdn-cian.ru/b.jpg"]),
        channel="test", use_custom_emoji=True,
    )
    assert len(requests) == 3 and len(bot.album_calls) == 1
    media = bot.album_calls[0]["media"]
    assert len(media) == 2 and not bot.message_calls
    for item in media:
        assert isinstance(item.media, BufferedInputFile)
        with Image.open(BytesIO(item.media.data)) as picture:
            assert picture.format == "JPEG" and picture.size == (800, 600)
    assert bot.markup_calls  # Navigation remains attached to the album.
