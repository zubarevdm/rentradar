"""Choose real source photo variants; never upscale or invent signed CDN URLs."""

import html
import re
import struct
from io import BytesIO

from PIL import Image, ImageOps


def telegram_photo(data: bytes) -> tuple[bytes, str]:
    """Validate a source image; convert WebP to JPEG without resizing.

    Cian's .jpg URLs can return WebP. Telegram cannot reliably fetch those URLs
    or accept that payload as a photo. Preserve original JPEG/PNG bytes.
    """
    with Image.open(BytesIO(data)) as original:
        original.load()
        if original.format in ("JPEG", "PNG"):
            return data, "jpg" if original.format == "JPEG" else "png"
        if original.format != "WEBP":
            raise ValueError(f"Unsupported photo format: {original.format}")
        converted = ImageOps.exif_transpose(original).convert("RGB")
        output = BytesIO()
        converted.save(output, format="JPEG", quality=95)
        return output.getvalue(), "jpg"

_OBJECT = re.compile(r"\{[^{}]*\}")
_SIZE_URL = re.compile(r'"(\d{2,4})x(\d{2,4})"\s*:\s*"(https?://[^"\s]+)"')


def detail_photos(document: str, limit: int) -> list[str]:
    # Avito embeds both JSON and JSON serialized inside script strings. Decode
    # only JSON escapes used by these keys/URLs, without parsing arbitrary JS.
    if limit <= 0:
        return []
    text = document.replace(r"\"", '"').replace(r"\/", "/").replace(r"\u002F", "/")
    selected: list[str] = []
    for match in _OBJECT.finditer(text):
        candidates = [
            (int(w) * int(h), html.unescape(url))
            for w, h, url in _SIZE_URL.findall(match.group())
            if max(int(w), int(h)) >= 640 and int(w) + int(h) <= 10000
        ]
        if not candidates:
            continue
        _, url = max(candidates, key=lambda item: item[0])
        if url not in selected:
            selected.append(url)
        if len(selected) >= limit:
            break
    return selected


def largest_srcset(value: str) -> tuple[float, str] | None:
    candidates: list[tuple[float, str]] = []
    for part in value.split(","):
        match = re.fullmatch(r"\s*(https?://\S+)\s+(\d+(?:\.\d+)?)([wx])\s*", part)
        if match:
            url, amount, unit = match.groups()
            # Width and density descriptors must not be mixed in one srcset.
            score = float(amount) * (1000 if unit == "x" else 1)
            candidates.append((score, html.unescape(url)))
    return max(candidates, key=lambda item: item[0]) if candidates else None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read JPEG/PNG dimensions without decoding or recompressing source bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if not data.startswith(b"\xff\xd8"):
        return None
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            return None
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return None
        marker = data[offset]
        offset += 1
        if marker in (0xD8, 0xD9, 0xDA):
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            return None
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC) and length >= 7:
            height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
            return width, height
        offset += length
    return None
