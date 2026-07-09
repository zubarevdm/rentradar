"""Тесты парсеров пользовательского ввода и сборки роутера бота."""

from __future__ import annotations

from rentradar.personal.parsing import (
    parse_csv,
    parse_int_or_none,
    parse_price_range,
    parse_rooms,
)


def test_parse_rooms() -> None:
    assert parse_rooms("студия, 1, 2") == [0, 1, 2]
    assert parse_rooms("1 2 3") == [1, 2, 3]
    assert parse_rooms("4+") == [4]
    assert parse_rooms("пропустить") == []
    assert parse_rooms("2, 2, 1") == [1, 2]  # дубли убираются


def test_parse_price_range() -> None:
    assert parse_price_range("30000-70000") == (30000, 70000)
    assert parse_price_range("70000-30000") == (30000, 70000)  # порядок неважен
    assert parse_price_range("до 60000") == (None, 60000)
    assert parse_price_range("от 40000") == (40000, None)
    assert parse_price_range("55000") == (None, 55000)  # одно число → максимум
    assert parse_price_range("пропустить") == (None, None)


def test_parse_csv() -> None:
    assert parse_csv("Парк культуры, Фрунзенская") == ["Парк культуры", "Фрунзенская"]
    assert parse_csv("пропустить") == []
    assert parse_csv("-") == []


def test_parse_int_or_none() -> None:
    assert parse_int_or_none("10") == 10
    assert parse_int_or_none("≤ 7 мин") == 7
    assert parse_int_or_none("пропустить") is None


def test_build_router_constructs() -> None:
    # Импортируем здесь, чтобы тесты парсинга не тянули aiogram, если он не нужен.
    from rentradar.config import Settings
    from rentradar.personal.bot import build_router

    class _FakeStore:
        pass

    router = build_router(_FakeStore(), Settings(yookassa_provider_token=""))  # type: ignore[arg-type]
    assert router is not None
