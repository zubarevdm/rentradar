"""Тесты нормализации Авито (разбор заголовка, поля). Без сети."""

from __future__ import annotations

from rentradar.collectors import AvitoCollector
from rentradar.models import PropertyType, RawListing, Source


def _raw(title: str, price: int, **extra: object) -> RawListing:
    payload = {
        "id": "1",
        "url": "https://www.avito.ru/moskva/kvartiry/x_1",
        "title": title,
        "price": price,
        "address": extra.get("address"),
        "metro": extra.get("metro"),
        "photos": list(extra.get("photos", ["p1", "p2"])),  # type: ignore[arg-type]
    }
    return RawListing(
        source=Source.AVITO, external_id="1", url=payload["url"], payload=payload
    )


def test_normalize_basic() -> None:
    c = AvitoCollector()
    listing = c.normalize(
        _raw("2-к. квартира, 40 м², 2/14 эт.", 53_000, metro="Отрадное", address="ул. X, 1")
    )
    assert listing is not None
    assert listing.source == Source.AVITO
    assert listing.rooms == 2
    assert listing.area == 40.0
    assert listing.price == 53_000
    assert listing.floor == 2
    assert listing.floors_total == 14
    assert listing.metro == "Отрадное"
    assert listing.property_type == PropertyType.FLAT


def test_normalize_studio() -> None:
    c = AvitoCollector()
    listing = c.normalize(_raw("Студия, 20,5 м², 4/33 эт.", 65_000))
    assert listing is not None
    assert listing.rooms == 0
    assert listing.area == 20.5
    assert listing.property_type == PropertyType.STUDIO


def test_normalize_room_and_unknown() -> None:
    c = AvitoCollector()
    room = c.normalize(_raw("Комната, 12 м², 3/9 эт.", 25_000))
    assert room is not None and room.rooms is None and room.property_type == PropertyType.ROOM
    apart = c.normalize(_raw("Апартаменты, 30 м², 5/20 эт.", 70_000))
    assert apart is not None and apart.property_type == PropertyType.UNKNOWN


def test_normalize_rejects_nonpositive_price() -> None:
    c = AvitoCollector()
    assert c.normalize(_raw("1-к. квартира, 30 м², 1/5 эт.", 0)) is None


# ── детальная страница: залог/комиссия/описание (для «Заехать» и скам-гейта) ──
def test_parse_deposit_commission_variants() -> None:
    from rentradar.collectors.avito import _parse_deposit_commission as p

    # Как в JSON страницы: &nbsp; между словами, «без комиссии».
    assert p("залог\u0026nbsp;125000\u0026nbsp;₽, без\u0026nbsp;комиссии", 100_000) == (125000, 0)
    assert p("залог 90000 ₽, комиссия 50%", 90_000) == (90000, 50)
    # Комиссия в рублях → переводим в % через цену.
    assert p("залог 50000 ₽, комиссия 40 000 ₽", 50_000) == (50000, 80)
    assert p("без залога, без комиссии", 50_000) == (0, 0)
    # Комиссия в рублях без цены — процент не посчитать.
    assert p("комиссия 30 000 ₽", None) == (None, None)


def test_parse_detail_extracts_all_fields() -> None:
    from rentradar.collectors.avito import parse_detail

    html = (
        '<html><body>'
        '<div data-marker="item-view/item-description"><p>Сдаётся уютная квартира.'
        '</p><p>От собственника.</p></div>'
        '<script>x={\\"images\\":{\\"1280x960\\":\\"https://img.example/1.jpg\\"},'
        '\\"depositCommission\\":\\"залог\u0026nbsp;50000\u0026nbsp;₽, комиссия'
        '\u0026nbsp;40\u0026nbsp;000\u0026nbsp;₽\\"}</script>'
        '</body></html>'
    )
    d = parse_detail(html, price=50_000)
    assert d.photos == ["https://img.example/1.jpg"]
    assert d.deposit_rub == 50000
    assert d.commission_pct == 80
    assert "Сдаётся уютная квартира." in (d.description or "")
    assert "От собственника." in (d.description or "")


def test_parse_detail_empty_page() -> None:
    from rentradar.collectors.avito import parse_detail

    d = parse_detail("<html><body>ничего нет</body></html>")
    assert d.photos == [] and d.deposit_rub is None
    assert d.commission_pct is None and d.description is None
