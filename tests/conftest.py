"""Общие фикстуры тестов."""

from __future__ import annotations

from datetime import datetime

import pytest

from rentradar.models import DealType, Listing, PropertyType, Source


@pytest.fixture(autouse=True)
def isolated_avito_transport(monkeypatch):
    from rentradar.collectors import avito, avito_access

    for name in (
        "_blocked_until", "_block_attempts", "_pending_recovery",
        "_last_request_at", "_request_locks",
    ):
        monkeypatch.setattr(avito, name, {})
    monkeypatch.setattr(avito, "_request_delay_seconds", 0.0)
    monkeypatch.setattr(avito, "_block_cooldown_seconds", 720.0)
    monkeypatch.setattr(avito_access, "_proxy_locks", {})
    avito_access.configure(None)
    yield
    avito_access.configure(None)


@pytest.fixture
def sample_listing() -> Listing:
    return Listing(
        source=Source.CIAN,
        external_id="123456",
        url="https://www.cian.ru/rent/flat/123456/",
        deal_type=DealType.LONG_RENT,
        property_type=PropertyType.FLAT,
        price=55_000,
        area=38.0,
        rooms=1,
        city="Москва",
        district="Хамовники",
        metro="Парк культуры",
        metro_distance_min=7,
        photos=["https://example.com/1.jpg"],
        collected_at=datetime(2026, 6, 21, 12, 0, 0),
        published_at=datetime(2026, 6, 21, 11, 30, 0),
    )
