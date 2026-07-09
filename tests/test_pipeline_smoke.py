"""Smoke-тест конвейера на моках: collect → dedup → score → publish (dry-run).

Проверяет, что слои корректно стыкуются через интерфейсы и что повторный лот
не публикуется дважды (идемпотентность).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from rentradar.config import SearchProfile, Settings
from rentradar.interfaces import BaseCollector
from rentradar.models import DealType, Listing, PropertyType, RawListing, Source
from rentradar.pipeline import Pipeline
from rentradar.publisher import TelegramPublisher
from rentradar.scoring import PlaceholderScoringEngine
from rentradar.storage import SqlStorage


class FakeCollector(BaseCollector):
    source_name = "fake"

    def __init__(self, listings: list[Listing]) -> None:
        self._listings = listings

    async def fetch(self, profile: SearchProfile, *, limit: int = 50) -> list[RawListing]:
        return [
            RawListing(source=lst.source, external_id=lst.external_id, url=lst.url)
            for lst in self._listings
        ]

    def normalize(self, raw: RawListing) -> Listing | None:
        return next(lst for lst in self._listings if lst.external_id == raw.external_id)


def _make_listing(ext_id: str, price: int, address: str | None = None) -> Listing:
    return Listing(
        source=Source.CIAN,
        external_id=ext_id,
        url=f"https://cian.ru/{ext_id}",
        deal_type=DealType.LONG_RENT,
        property_type=PropertyType.FLAT,
        price=price,
        area=40.0,
        rooms=1,
        city="Москва",
        district="Хамовники",
        address=address or f"ул. Тестовая, {ext_id}",
        photos=["p.jpg"],
        collected_at=datetime(2026, 6, 21, 12, 0, 0),
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        dry_run=True,
        public_channel="@test_channel",
    )


async def _build_pipeline(
    settings: Settings, listings: list[Listing], max_per_cycle: int | None = None
) -> tuple[Pipeline, SqlStorage]:
    storage = SqlStorage(settings.db_url, db_path=settings.db_path)
    await storage.init()
    pipeline = Pipeline(
        collectors=[FakeCollector(listings)],
        storage=storage,
        scoring=PlaceholderScoringEngine(),
        publisher=TelegramPublisher(dry_run=True),
        settings=settings,
        post_delay=0,
        max_per_cycle=max_per_cycle,
    )
    return pipeline, storage


async def test_collect_only_reports_status(settings: Settings) -> None:
    # collect_only возвращает (число новых, {source: 'ok'|...}) для health-мониторинга.
    profile = SearchProfile(name="t", city="Москва", publish_threshold=40.0)
    listings = [_make_listing("1", 50_000), _make_listing("2", 60_000)]
    pipeline, storage = await _build_pipeline(settings, listings)
    try:
        new, statuses = await pipeline.collect_only(profile)
        assert new == 2
        assert statuses == {"fake": "ok"}
        # повторный сбор — тех же нет как новых, но площадка всё равно 'ok'
        new2, statuses2 = await pipeline.collect_only(profile)
        assert new2 == 0
        assert statuses2 == {"fake": "ok"}
    finally:
        await storage.dispose()


async def test_collect_only_marks_blocked(settings: Settings) -> None:
    from rentradar.errors import CollectorBlockedError

    class BlockedCollector(BaseCollector):
        source_name = "avito"

        async def fetch(self, profile, *, limit=50):
            raise CollectorBlockedError("бан")

        def normalize(self, raw):
            return None

    storage = SqlStorage(settings.db_url, db_path=settings.db_path)
    await storage.init()
    pipeline = Pipeline(
        collectors=[BlockedCollector()],
        storage=storage,
        scoring=PlaceholderScoringEngine(),
        publisher=TelegramPublisher(dry_run=True),
        settings=settings,
    )
    try:
        new, statuses = await pipeline.collect_only(SearchProfile(name="t", city="Москва"))
        assert new == 0
        assert statuses == {"avito": "blocked"}
    finally:
        await storage.dispose()


async def test_pipeline_publishes_fresh(settings: Settings) -> None:
    profile = SearchProfile(name="t", city="Москва", publish_threshold=40.0)
    listings = [_make_listing("1", 50_000), _make_listing("2", 60_000)]
    pipeline, storage = await _build_pipeline(settings, listings)
    try:
        published = await pipeline.run_once(profile)
        assert len(published) == 2
    finally:
        await storage.dispose()


async def test_pipeline_dedup_no_double_post(settings: Settings) -> None:
    profile = SearchProfile(name="t", city="Москва", publish_threshold=40.0)
    listings = [_make_listing("1", 50_000)]
    pipeline, storage = await _build_pipeline(settings, listings)
    try:
        first = await pipeline.run_once(profile)
        second = await pipeline.run_once(profile)  # тот же лот снова
        assert len(first) == 1
        assert len(second) == 0  # дедуп: повторно не публикуем
    finally:
        await storage.dispose()


async def test_pipeline_dedup_similar_same_flat(settings: Settings) -> None:
    # Одна квартира (тот же адрес/площадь/комнаты), два объявления разной цены —
    # публикуем один раз (схлопывание по content_key).
    profile = SearchProfile(name="t", city="Москва", publish_threshold=40.0)
    listings = [
        _make_listing("1", 50_000, address="ул. Льва Толстого, 16"),
        _make_listing("2", 52_000, address="ул. Льва Толстого, 16"),
    ]
    pipeline, storage = await _build_pipeline(settings, listings)
    try:
        published = await pipeline.run_once(profile)
        assert len(published) == 1
    finally:
        await storage.dispose()


async def test_pipeline_caps_posts_per_cycle(settings: Settings) -> None:
    # 4 разных лота, но лимит 2 на цикл → публикуем только 2 (топ-N).
    profile = SearchProfile(name="t", city="Москва", publish_threshold=40.0)
    listings = [_make_listing(str(i), 50_000 + i * 1000) for i in range(4)]
    pipeline, storage = await _build_pipeline(settings, listings, max_per_cycle=2)
    try:
        published = await pipeline.run_once(profile)
        assert len(published) == 2
    finally:
        await storage.dispose()


async def test_pipeline_threshold_filters(settings: Settings) -> None:
    # Порог выше нейтрального скора 50 → ничего не публикуется.
    profile = SearchProfile(name="t", city="Москва", publish_threshold=80.0)
    listings = [_make_listing("1", 50_000)]
    pipeline, storage = await _build_pipeline(settings, listings)
    try:
        published = await pipeline.run_once(profile)
        assert published == []
    finally:
        await storage.dispose()
