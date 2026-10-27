"""Real larger source variants are selected without creating/enlarging images."""

import pytest
from selectolax.parser import HTMLParser

from rentradar.collectors.avito import _card_photos, parse_detail
from rentradar.photos import detail_photos, largest_srcset


@pytest.mark.parametrize("escaped", [False, True])
def test_detail_preserves_order_and_selects_largest_per_photo(escaped):
    text = (
        '{"images":[{"208x208":"https://img/a-thumb.jpg",'
        '"1280x960":"https://img/a-full.jpg","640x480":"https://img/a-medium.jpg"},'
        '{"640x960":"https://img/b-portrait.jpg","208x208":"https://img/b-thumb.jpg"}]}'
    )
    if escaped:
        text = text.replace('"', r"\"")
    assert detail_photos(text, 7) == ["https://img/a-full.jpg", "https://img/b-portrait.jpg"]
    assert parse_detail(text).photos == ["https://img/a-full.jpg", "https://img/b-portrait.jpg"]
    assert len(detail_photos(text, 1)) == 1


def test_detail_escaped_slashes_and_entities():
    text = r'{"1920x1080":"https:\/\/img\/photo.jpg?a=1&amp;b=2"}'
    assert detail_photos(text, 7) == ["https://img/photo.jpg?a=1&b=2"]


def test_thumbnails_do_not_masquerade_as_full_photos():
    assert detail_photos('{"208x208":"https://img/tiny.jpg"}', 7) == []
    assert detail_photos("<html>blocked</html>", 7) == []


def test_card_srcset_large_version_without_extra_requests():
    card = HTMLParser(
        '<div data-marker="slider-image/image-https://img/208.jpg">'
        '<img src="https://img/208.jpg" srcset="https://img/208.jpg 208w, '
        'https://img/1280.jpg 1280w, https://img/640.jpg 640w"></div>'
    )
    assert _card_photos(card.body) == ["https://img/1280.jpg"]
    assert largest_srcset("https://img/one 1x, https://img/two 2x") == (2000, "https://img/two")


def test_card_without_srcset_preserves_original_urls():
    card = HTMLParser('<div data-marker="slider-image/image-https://img/original.jpg"></div>')
    assert _card_photos(card.body) == ["https://img/original.jpg"]


def test_read_dimensions_of_real_jpeg_without_decoding():
    from pathlib import Path

    from rentradar.photos import image_dimensions

    fixtures = Path(__file__).parent / "fixtures"
    assert image_dimensions((fixtures / "photo-preview.jpg").read_bytes()) == (208, 208)
    assert image_dimensions((fixtures / "photo-large.jpg").read_bytes()) == (1280, 960)
    assert image_dimensions(b"not an image") is None
    assert image_dimensions(b"\xff\xd8\xff\xe0\xff\xff") is None
