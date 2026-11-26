"""One collection process per source, with SQLite control and health state."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from .collectors import AvitoCollector, CianCollector, YandexCollector, avito_access
from .collectors.avito import set_request_cooldown
from .config import SearchProfile, Settings
from .errors import CollectionSkipped
from .interfaces import BaseCollector
from .models import Source
from .pipeline import CollectionProgress, collect_source
from .runtime import mobile_request, notify_systemd
from .storage import SqlStorage
from .storage.orm import CollectorStateRow

log = logging.getLogger(__name__)
SOURCES = tuple(s.value for s in (Source.AVITO, Source.CIAN, Source.YANDEX))
DETAIL_ROLE = "avito_details"
HEARTBEAT_SECONDS = 5
STALE_AFTER = timedelta(seconds=20)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def build_collector(settings: Settings, source: str) -> BaseCollector:
    if source == "cian":
        return CianCollector(fallback_proxy=settings.cian_fallback_proxy or None)
    if source == "yandex":
        return YandexCollector(fallback_proxy=settings.yandex_fallback_proxy or None)
    if source != "avito":
        raise ValueError(f"Unknown source {source}")
    configure_avito(settings)
    return AvitoCollector(
        proxy=settings.avito_proxy or None, max_pages=settings.avito_max_pages,
        min_interval_sec=settings.avito_collect_interval_min * 60,
    )


def configure_avito(settings: Settings) -> None:
    set_request_cooldown(settings.avito_collect_interval_min * 60)
    if settings.avito_browser_warmup and settings.avito_proxy and avito_access.current() is None:
        avito_access.configure(avito_access.AvitoAccess(
            proxy=settings.avito_proxy,
            cookies_path=settings.avito_cookies_path,
            profile_dir=settings.avito_browser_profile,
            rotate_cmd=settings.avito_rotate_cmd,
            rotate_wait_sec=settings.avito_rotate_wait_sec,
            warm_timeout=settings.avito_browser_timeout_sec,
        ))


def source_enabled(settings: Settings, source: str) -> bool:
    return source != "avito" or settings.avito_enabled


def interval_seconds(settings: Settings, source: str) -> float:
    intervals = {"avito": settings.avito_collect_interval_min,
                 "yandex": settings.yandex_collect_interval_min}
    return 60 * max(settings.collect_interval_min, intervals.get(source, 0), 1)


class WorkerState:
    def __init__(self, storage: SqlStorage):
        self._session = storage._session

    async def read(self, *, include_details: bool = False) -> dict[str, CollectorStateRow]:
        sources = (*SOURCES, DETAIL_ROLE) if include_details else SOURCES
        async with self._session() as session:
            rows = await session.scalars(select(CollectorStateRow).where(
                CollectorStateRow.source.in_(sources)
            ))
            return {row.source: row for row in rows}

    async def request(self, source: str, now: datetime) -> str:
        async with self._session() as session:
            accepted = await session.execute(update(CollectorStateRow).where(
                CollectorStateRow.source == source,
                CollectorStateRow.heartbeat >= now - STALE_AFTER,
                CollectorStateRow.state.not_in(("running", "disabled", "stopped", "timeout")),
                CollectorStateRow.requested.is_(False),
            ).values(requested=True))
            row = await session.get(CollectorStateRow, source)
            await session.commit()
            if accepted.rowcount:
                return "requested"
            if (row is None or row.heartbeat < now - STALE_AFTER
                    or row.state in ("stopped", "timeout")):
                return "offline"
            if row.state == "disabled":
                return "disabled"
            return "running" if row.state == "running" else "queued"

    async def register(self, source: str, state: str) -> None:
        async with self._session() as session:
            heartbeat = utcnow()
            await session.execute(insert(CollectorStateRow).values(
                source=source, heartbeat=heartbeat, state=state,
            ).on_conflict_do_update(
                index_elements=[CollectorStateRow.source],
                set_={"heartbeat": heartbeat, "state": state},
            ))
            await session.commit()

    async def set(self, source: str, **values) -> None:
        async with self._session() as session:
            await session.execute(update(CollectorStateRow).where(
                CollectorStateRow.source == source
            ).values(**values))
            await session.commit()

    async def get(self, source: str) -> CollectorStateRow:
        async with self._session() as session:
            row = await session.get(CollectorStateRow, source)
            assert row is not None
            return row


def _classify(statuses: list[str], progress: CollectionProgress) -> str:
    observed = set(statuses) - {"skipped"}
    if observed == {"ok"}:
        return "ok" if progress.seen else "empty"
    if "ok" in observed or "partial" in observed:
        return "partial"
    if "blocked" in observed:
        return "blocked"
    return "error" if observed else "paused"


async def run_worker(
    settings: Settings, source: str, profiles: list[SearchProfile], storage: SqlStorage,
    *, collector: BaseCollector | None = None,
) -> None:
    states = WorkerState(storage)
    enabled = source_enabled(settings, source)
    interval = interval_seconds(settings, source)
    await states.register(source, "starting" if enabled else "disabled")
    previous = await states.get(source)
    if not enabled:
        notify_systemd("READY=1")
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await states.set(source, heartbeat=utcnow())
            notify_systemd("WATCHDOG=1")
    if collector is None:
        collector = build_collector(settings, source)
    phone = (
        avito_access.PhoneMonitor(
            proxy=settings.avito_proxy, control_cmd=settings.avito_phone_check_cmd,
        )
        if source == "avito" and settings.avito_browser_warmup and settings.avito_proxy
        else None
    )
    loop = asyncio.get_running_loop()
    due = loop.time()
    if previous.started_at is not None:
        due += max(0, interval - (utcnow() - previous.started_at).total_seconds())
    phone_due = loop.time()

    async def heartbeat() -> None:
        next_beat = loop.time()
        while True:
            begin = loop.time()
            lag = begin - next_beat
            await states.set(source, heartbeat=utcnow())
            write = loop.time() - begin
            if max(lag, write) >= 1:
                log.warning("heartbeat source=%s lag_ms=%d write_ms=%d",
                            source, round(lag * 1000), round(write * 1000))
            notify_systemd("WATCHDOG=1")
            next_beat = loop.time() + HEARTBEAT_SECONDS
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def passes() -> None:
        nonlocal due, phone_due
        while True:
            row = await states.get(source)
            if loop.time() >= due or row.requested:
                started = utcnow()
                begin = loop.time()
                due = begin + interval
                run_id = uuid4().hex[:12]
                await states.set(source, started_at=started, state="running", requested=False)
                progress = CollectionProgress()
                try:
                    async with asyncio.timeout(settings.worker_pass_timeout_sec):
                        statuses = []
                        for profile in profiles:
                            _, status = await collect_source(
                                storage, collector, profile, progress=progress,
                            )
                            statuses.append(status)
                except TimeoutError:
                    elapsed = round((loop.time() - begin) * 1000)
                    await states.set(
                        source, state="timeout", failures=row.failures + 1,
                        new_count=progress.new, changed_count=progress.changed,
                        reason="pass_timeout", duration_ms=elapsed,
                        last_usable=utcnow() if progress.pages else row.last_usable,
                    )
                    log.warning("collection_timeout source=%s run_id=%s duration_ms=%d "
                                "pages=%d new=%d changed=%d rejected=%d", source, run_id, elapsed,
                                progress.pages, progress.new, progress.changed, progress.rejected)
                    raise
                state = _classify(statuses, progress)
                succeeded = state in ("ok", "empty", "partial")
                failures = 0 if succeeded else row.failures + (state != "paused")
                finished = utcnow()
                elapsed = round((loop.time() - begin) * 1000)
                await states.set(
                    source, state=state, new_count=progress.new, changed_count=progress.changed,
                    last_success=finished if state in ("ok", "empty") else row.last_success,
                    last_usable=finished if succeeded else row.last_usable,
                    failures=failures, reason=progress.reason, duration_ms=elapsed,
                )
                log.info("collection source=%s state=%s new=%d changed=%d total=%d "
                         "run_id=%s duration_ms=%d pages=%d rejected=%d reason=%s",
                         source, state, progress.new, progress.changed, progress.seen, run_id,
                         elapsed, progress.pages, progress.rejected, progress.reason)
            if phone is not None and loop.time() >= phone_due:
                phone_due = loop.time() + 60 * max(1, settings.avito_phone_check_min)
                try:
                    async with asyncio.timeout(60), mobile_request(settings.avito_proxy):
                        changes = dict(await phone.check())
                    if changes:
                        await states.set(source, phone={**(row.phone or {}), **changes})
                except (TimeoutError, CollectionSkipped):
                    log.warning("avito phone check postponed")
            await asyncio.sleep(1)

    notify_systemd("READY=1")
    try:
        # A blocked event loop stops watchdog notifications. A stalled async pass
        # raises above. Both recovery paths restart this process only.
        async with asyncio.TaskGroup() as group:
            group.create_task(heartbeat())
            group.create_task(passes())
    except asyncio.CancelledError:
        await states.set(source, state="stopped", heartbeat=utcnow(), requested=False)
        raise
