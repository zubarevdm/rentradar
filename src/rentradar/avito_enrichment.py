"""Durable detail enrichment, independent of search-page collection."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from .collectors.avito import _in_cooldown, fetch_detail
from .config import Settings
from .errors import CollectionSkipped, DetailDeferred
from .personal.filters import matches
from .personal.store import PersonalStore
from .runtime import notify_systemd
from .storage import SqlStorage
from .worker import (
    DETAIL_ROLE,
    HEARTBEAT_SECONDS,
    WorkerState,
    configure_avito,
    interval_seconds,
    utcnow,
)

log = logging.getLogger(__name__)


async def enrich_avito(settings: Settings, storage: SqlStorage) -> int:
    cap = settings.avito_detail_max_per_run
    proxy = settings.avito_proxy or None
    if cap <= 0 or _in_cooldown(proxy):
        return 0
    now = utcnow()
    store = PersonalStore(storage, pause_max_days=settings.pause_max_days)
    access = {}
    filters = []
    for flt in await store.all_active_filters():
        if flt.user_id not in access:
            access[flt.user_id] = await store.is_active(flt.user_id, now)
        if access[flt.user_id]:
            filters.append((flt, flt.model_copy(update={"no_commission": False})))
    candidates = await storage.ready_details(now, max(60, cap * 15))
    candidates.sort(key=lambda item: not any(
        matches(item, provisional if item.commission_amount is None else flt)
        for flt, provisional in filters
    ))
    saved = 0
    for index, candidate in enumerate(candidates[:cap]):
        if index:
            await asyncio.sleep(settings.avito_detail_delay_sec)
        if _in_cooldown(proxy):
            break
        claimed = await storage.claim_detail(candidate, utcnow(),
                                             settings.avito_detail_run_timeout_sec + 30)
        if claimed is None:
            continue
        claim, listing = claimed
        try:
            detail = await fetch_detail(listing.url, price=listing.price, proxy=proxy,
                                        parse_timeout=settings.avito_detail_parse_timeout_sec)
        except CollectionSkipped:
            await storage.defer_detail(claim, utcnow() + timedelta(minutes=2), "proxy_busy",
                                       count_attempt=False)
            break
        except DetailDeferred as error:
            delay = timedelta(minutes=min(120, 5 * 2 ** claim.attempts))
            await storage.defer_detail(claim, utcnow() + delay, str(error), count_attempt=True)
            log.warning("avito detail stage=parse result=deferred reason=%s", error)
            continue
        except asyncio.CancelledError:
            await storage.defer_detail(claim, utcnow() + timedelta(minutes=5), "interrupted",
                                       count_attempt=False)
            raise
        if detail is None:
            paused = _in_cooldown(proxy)
            await storage.defer_detail(claim, utcnow() + timedelta(minutes=10),
                                       "cooldown" if paused else "detail_unavailable",
                                       count_attempt=not paused)
            if paused:
                break
            continue
        changed = await storage.save_enrichment(
            "avito", listing.external_id, enriched_at=utcnow(), claim=claim, **detail._asdict(),
        )
        saved += changed is not None
    return saved


async def run_avito_enrichment(settings: Settings, storage: SqlStorage) -> None:
    states = WorkerState(storage)
    enabled = settings.avito_enabled and settings.avito_detail_max_per_run > 0
    await states.register(DETAIL_ROLE, "starting" if enabled else "disabled")
    if enabled:
        configure_avito(settings)
        await storage.seed_details(utcnow() - timedelta(hours=24))

    async def heartbeat() -> None:
        while True:
            await states.set(DETAIL_ROLE, heartbeat=utcnow())
            notify_systemd("WATCHDOG=1")
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def passes() -> None:
        loop = asyncio.get_running_loop()
        interval = interval_seconds(settings, "avito")
        # Give the search service its first slot when both start together.
        await asyncio.sleep(min(45, interval / 2))
        while enabled:
            start, due = utcnow(), loop.time() + interval
            await states.set(DETAIL_ROLE, started_at=start, state="running")
            try:
                async with asyncio.timeout(settings.avito_detail_run_timeout_sec):
                    changed = await enrich_avito(settings, storage)
            except TimeoutError:
                await states.set(DETAIL_ROLE, state="timeout")
                log.warning("avito detail result=timeout duration_seconds=%.2f",
                            loop.time() - (due - interval))
            else:
                status = "ok" if changed else "idle"
                await states.set(DETAIL_ROLE, state=status, changed_count=changed,
                                 **({"last_success": utcnow()} if changed else {}))
                log.info("avito detail result=%s changed=%d duration_seconds=%.2f",
                         status, changed, loop.time() - (due - interval))
            await storage.prune_details(utcnow() - timedelta(hours=24))
            await asyncio.sleep(max(0, due - loop.time()))

    notify_systemd("READY=1")
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(heartbeat())
            group.create_task(passes())
    except asyncio.CancelledError:
        await states.set(DETAIL_ROLE, state="stopped", heartbeat=utcnow())
        raise
