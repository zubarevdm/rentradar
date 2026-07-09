"""Тесты поиска станций метро (подсказки + опечатки) и выборки из БД."""

from __future__ import annotations

from datetime import datetime

import pytest

from rentradar.models import DealType, Listing, PropertyType, Source
from rentradar.personal import PersonalStore
from rentradar.personal.metro import suggest_metros
from rentradar.storage import SqlStorage

STATIONS = [
    "Профсоюзная",
    "Парк культуры",
    "Фрунзенская",
    "Проспект Вернадского",
    "Парк Победы",
    "Павелецкая",
]


def test_substring_match() -> None:
    assert "Профсоюзная" in suggest_metros("профсоюз", STATIONS)
    # «парк» → обе парковые станции
    res = suggest_metros("парк", STATIONS)
    assert "Парк культуры" in res and "Парк Победы" in res


def test_typo_tolerant() -> None:
    # опечатка «прафсаюзная» всё равно находит «Профсоюзная»
    assert "Профсоюзная" in suggest_metros("прафсаюзная", STATIONS)


def test_empty_query() -> None:
    assert suggest_metros("", STATIONS) == []


def test_limit() -> None:
    assert len(suggest_metros("п", STATIONS, limit=2)) <= 2


@pytest.fixture
async def storage(tmp_path):
    st = SqlStorage(f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}")
    await st.init()
    yield st
    await st.dispose()


async def test_distinct_metros_from_data(storage: SqlStorage) -> None:
    def mk(ext: str, metro: str | None) -> Listing:
        return Listing(
            source=Source.CIAN,
            external_id=ext,
            url=f"u{ext}",
            deal_type=DealType.LONG_RENT,
            property_type=PropertyType.FLAT,
            price=50000,
            area=40.0,
            rooms=1,
            city="Москва",
            address=f"ул. {ext}",
            metro=metro,
            collected_at=datetime(2026, 6, 21, 12, 0, 0),
        )

    await storage.upsert_listings(
        [mk("1", "Профсоюзная"), mk("2", "Профсоюзная"), mk("3", "Парк культуры"), mk("4", None)]
    )
    ps = PersonalStore(storage)
    metros = await ps.distinct_metros("Москва")
    assert metros == ["Парк культуры", "Профсоюзная"]  # уникальные, без None, отсортированы
