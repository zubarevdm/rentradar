"""Тесты доменных моделей: производные поля и стабильность отпечатков."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from rentradar.models import Listing, Source


def test_price_per_m2(sample_listing: Listing) -> None:
    assert sample_listing.price_per_m2 == pytest.approx(55_000 / 38.0)


def test_price_per_m2_none_without_area(sample_listing: Listing) -> None:
    no_area = sample_listing.model_copy(update={"area": None})
    assert no_area.price_per_m2 is None


def test_fingerprint_is_stable(sample_listing: Listing) -> None:
    twin = sample_listing.model_copy(update={"external_id": "999"})
    # Тот же объект (адрес/площадь/комнаты/цена-бакет) → тот же отпечаток.
    assert sample_listing.fingerprint == twin.fingerprint


def test_fingerprint_changes_with_price(sample_listing: Listing) -> None:
    pricier = sample_listing.model_copy(update={"price": 99_000})
    assert sample_listing.fingerprint != pricier.fingerprint


def test_segment_key_groups_similar(sample_listing: Listing) -> None:
    # Площадь в том же 10-метровом бакете → тот же сегмент.
    similar = sample_listing.model_copy(update={"area": 39.0, "price": 60_000})
    assert sample_listing.segment_key == similar.segment_key


def test_price_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Listing(
            source=Source.CIAN,
            external_id="1",
            url="u",
            price=0,
            city="Москва",
            collected_at=datetime(2026, 6, 21),
        )


def _same_flat(source: Source, price: int, ext: str) -> Listing:
    # Один и тот же адрес/комнаты/площадь → одинаковый content_key на всех площадках.
    return Listing(
        source=source, external_id=ext, url=f"http://{source.value}/{ext}",
        price=price, area=40.0, rooms=1, city="Москва", address="ул. Тестовая, 5",
        collected_at=datetime(2026, 7, 11, 12, 0, 0),
    )


def test_dedupe_cross_source_keeps_cheapest() -> None:
    from rentradar.models import dedupe_cross_source

    items = [
        _same_flat(Source.CIAN, 60_000, "c1"),
        _same_flat(Source.YANDEX, 55_000, "y1"),  # дешевле всех
        _same_flat(Source.AVITO, 62_000, "a1"),
    ]
    out = dedupe_cross_source(items)
    assert len(out) == 1
    assert out[0].source == Source.YANDEX and out[0].price == 55_000


def test_dedupe_cross_source_tie_priority_avito() -> None:
    from rentradar.models import dedupe_cross_source

    items = [
        _same_flat(Source.YANDEX, 60_000, "y1"),
        _same_flat(Source.CIAN, 60_000, "c1"),
        _same_flat(Source.AVITO, 60_000, "a1"),  # та же цена → приоритет Avito
    ]
    out = dedupe_cross_source(items)
    assert len(out) == 1 and out[0].source == Source.AVITO


def test_dedupe_cross_source_keeps_distinct_flats() -> None:
    from rentradar.models import dedupe_cross_source

    a = _same_flat(Source.CIAN, 60_000, "c1")
    b = _same_flat(Source.CIAN, 60_000, "c2").model_copy(update={"address": "ул. Другая, 9"})
    out = dedupe_cross_source([a, b])
    assert len(out) == 2  # разные адреса → разные квартиры, не схлопываем
