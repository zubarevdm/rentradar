"""Тесты весов престижности районов и их влияния на сигнал локации."""

from __future__ import annotations

from datetime import datetime

import pytest

from rentradar.models import DealType, Listing, PropertyType, Source
from rentradar.scoring.districts import district_weight
from rentradar.scoring.tasty import _location_signal


def test_known_districts_ranked() -> None:
    assert district_weight("Москва", "Хамовники") == 1.0
    assert district_weight("Москва", "Капотня") < 0.3
    # престижный центр выше окраины
    assert district_weight("Москва", "Арбат") > district_weight("Москва", "Люблино")


def test_unknown_and_other_city() -> None:
    assert district_weight("Москва", "Несуществующий") == 0.5
    assert district_weight("Москва", None) is None
    assert district_weight("Казань", "Вахитовский") is None  # таблицы для города нет


def test_new_moscow_penalised() -> None:
    assert district_weight("Москва", "НАО (Новомосковский)") == 0.35


def _listing(**over: object) -> Listing:
    base = {
        "source": Source.CIAN,
        "external_id": "1",
        "url": "u",
        "deal_type": DealType.LONG_RENT,
        "property_type": PropertyType.FLAT,
        "price": 55_000,
        "area": 38.0,
        "rooms": 1,
        "city": "Москва",
        "metro": "Парк культуры",
        "metro_distance_min": 7,
        "collected_at": datetime(2026, 6, 21, 12, 0, 0),
    }
    base.update(over)
    return Listing(**base)  # type: ignore[arg-type]


def test_location_blends_metro_and_district() -> None:
    # При одинаковом метро престижный район даёт более высокий сигнал локации.
    good, _ = _location_signal(_listing(district="Хамовники"))
    poor, _ = _location_signal(_listing(district="Капотня"))
    assert good > poor


def test_location_metro_only_when_no_district() -> None:
    val, _ = _location_signal(_listing(district=None, metro_distance_min=5))
    assert val == pytest.approx(1.0)
