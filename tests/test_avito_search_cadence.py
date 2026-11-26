from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from rentradar import worker
from rentradar.collectors import avito
from rentradar.config import SearchProfile, Settings
from rentradar.models import RawListing, Source
from rentradar.pipeline import CollectionProgress, collect_source
from rentradar.storage import SqlStorage
from rentradar.storage.orm import ListingEventRow


@asynccontextmanager
async def temporary_storage(path):
    storage = SqlStorage(f"sqlite+aiosqlite:///{path.as_posix()}")
    await storage.init()
    try:
        yield storage
    finally:
        await storage.dispose()


@pytest.mark.parametrize("manual", [False, True])
async def test_early_worker_tick_retries_at_collector_deadline(
    tmp_path, monkeypatch, caplog, manual,
):
    clock = 1000.0
    loop = SimpleNamespace(time=lambda: clock)
    async with temporary_storage(tmp_path / "cadence.db") as storage:
        states = worker.WorkerState(storage)
        calls = []
        finished = asyncio.Event()
        first_running = True
        manual_sent = False
        set_state = worker.WorkerState.set

        async def set_with_jitter(self, source, **values):
            nonlocal clock, first_running
            await set_state(self, source, **values)
            if values.get("state") == "running" and first_running:
                clock += 0.25  # Time between the first pass start and the adapter's gate.
                first_running = False
            if len(calls) == 2 and values.get("state") == "empty":
                finished.set()

        async def sleep(seconds):
            nonlocal clock, manual_sent
            if seconds == worker.HEARTBEAT_SECONDS:
                await asyncio.sleep(seconds)
                return
            if manual and not manual_sent and clock == 1000.25:
                assert await states.request("avito", worker.utcnow()) == "requested"
                manual_sent = True
            else:
                clock += seconds
            await asyncio.sleep(0)

        async def fetch(*args, **kwargs):
            calls.append(clock)
            return "<html></html>"

        monkeypatch.setattr(worker, "asyncio", SimpleNamespace(
            get_running_loop=lambda: loop, sleep=sleep,
            timeout=asyncio.timeout, TaskGroup=asyncio.TaskGroup,
            CancelledError=asyncio.CancelledError,
        ))
        monkeypatch.setattr(avito, "asyncio", SimpleNamespace(get_running_loop=lambda: loop))
        monkeypatch.setattr(avito, "_avito_fetch_html", fetch)
        monkeypatch.setattr(avito, "_in_cooldown", lambda _: False)
        monkeypatch.setattr(avito, "_parse_cards", lambda _: [])
        monkeypatch.setattr(worker.WorkerState, "set", set_with_jitter)
        caplog.set_level("INFO", logger=worker.__name__)
        profiles = [SearchProfile(name=name, city="Москва") for name in ("first", "second")]
        task = asyncio.create_task(worker.run_worker(
            Settings(_env_file=None, avito_collect_interval_min=3), "avito", profiles, storage,
            collector=avito.AvitoCollector(min_interval_sec=180, max_pages=1, min_delay=0),
        ))
        try:
            await asyncio.wait_for(finished.wait(), timeout=8)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert calls == [1000.25, 1180.25]
        assert "state=paused" in caplog.text
        assert "reason=interval" in caplog.text


async def test_single_page_pilot_commits_the_same_listings_as_six_minute_search(
    tmp_path, monkeypatch, sample_listing,
):
    cards = [RawListing(source=Source.AVITO, external_id=str(i), url=f"https://example.org/{i}",
                        payload={}) for i in range(1, 51)]
    fetch = AsyncMock(return_value="<html></html>")
    monkeypatch.setattr(avito, "_avito_fetch_html", fetch)
    monkeypatch.setattr(avito, "_in_cooldown", lambda _: False)
    monkeypatch.setattr(avito, "_parse_cards", lambda _: cards)
    monkeypatch.setattr(avito, "_wrap_card", lambda raw: raw)
    profiles = [SearchProfile(name=str(limit), city="Москва", collect_limit=limit)
                for limit in (50, 120)]
    for interval in (360, 180):
        collector = avito.AvitoCollector(min_interval_sec=interval, max_pages=1, min_delay=0)
        monkeypatch.setattr(collector, "normalize", lambda raw: sample_listing.model_copy(
            update={"source": raw.source, "external_id": raw.external_id, "url": raw.url},
        ))
        async with temporary_storage(tmp_path / f"search-{interval}.db") as storage:
            progress = CollectionProgress()
            _, status = await collect_source(storage, collector, profiles[0], progress=progress)
            assert status == "ok"
            _, status = await collect_source(storage, collector, profiles[1], progress=progress)
            assert status == "skipped"
            assert progress.pages == progress.pages_received == 1
            async with storage._session() as session:
                ids = set(await session.scalars(select(ListingEventRow.external_id)))
                assert ids == {raw.external_id for raw in cards}
    assert [call.kwargs["params"]["p"] for call in fetch.await_args_list] == [1, 1]


async def test_page_timeout_preserves_the_first_page_and_its_delivery_event(
    tmp_path, monkeypatch, sample_listing,
):
    raw = RawListing(source=Source.AVITO, external_id="1", url=sample_listing.url, payload={})
    monkeypatch.setattr(avito, "_avito_fetch_html", AsyncMock(
        side_effect=["<html></html>", TimeoutError("network deadline")],
    ))
    monkeypatch.setattr(avito, "_in_cooldown", lambda _: False)
    monkeypatch.setattr(avito, "_parse_cards", lambda _: [raw])
    monkeypatch.setattr(avito, "_wrap_card", lambda raw: raw)
    collector = avito.AvitoCollector(max_pages=2, min_delay=0)
    monkeypatch.setattr(collector, "normalize", lambda raw: sample_listing.model_copy(
        update={"source": raw.source, "external_id": raw.external_id},
    ))
    async with temporary_storage(tmp_path / "page-timeout.db") as storage:
        progress = CollectionProgress()
        _, status = await collect_source(
            storage, collector, SearchProfile(name="fixture", city="Москва"), progress=progress,
        )
        assert status == "partial"
        assert progress.pages == progress.pages_received == 1
        assert await storage.get_listing("avito", "1") is not None
        async with storage._session() as session:
            assert await session.scalar(select(ListingEventRow.external_id)) == "1"
