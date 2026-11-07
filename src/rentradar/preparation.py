"""Prepare Avito cards before matching and delivery, within one request budget."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .collectors.avito import fetch_detail
from .config import Settings
from .personal.store import PersonalStore
from .storage import SqlStorage

logger = logging.getLogger(__name__)


class AvitoPreparer:
    def __init__(
        self, storage: SqlStorage, store: PersonalStore, settings: Settings,
        on_ready: Callable[[], None],
    ) -> None:
        self.storage = storage
        self.store = store
        self.settings = settings
        self.on_ready = on_ready
        self._lock = asyncio.Lock()

    async def run(self) -> None:
        async with self._lock:
            now = datetime.now(UTC).replace(tzinfo=None)
            candidates = await self.storage.unenriched_listings(
                "avito", now - timedelta(hours=self.settings.personal_lookback_hours), None,
                retry_before=now - timedelta(minutes=30),
            )
            candidates = await self.store.pending_listings(candidates, now)
            fetched = 0
            budget = self.settings.avito_detail_max_per_run
            for listing in candidates:
                detail = None
                if budget > 0:
                    if fetched:
                        await asyncio.sleep(self.settings.avito_detail_delay_sec)
                    detail = await fetch_detail(
                        listing.url, price=listing.price,
                        proxy=self.settings.avito_proxy or None,
                    )
                    budget -= 1
                    if detail is None:
                        budget = 0
                ready_at = datetime.now(UTC).replace(tzinfo=None)
                if detail is not None:
                    await self.storage.save_enrichment(
                        listing.source.value, listing.external_id,
                        enriched_at=ready_at, **detail._asdict(),
                    )
                    fetched += 1
                else:
                    # Preserve the existing fallback using known search fields.
                    # The publisher must not spend another request per recipient.
                    await self.storage.mark_prepared(listing, ready_at)
                self.on_ready()
            if candidates:
                logger.info(
                    "Подготовка Авито: %d карточек, детали получены для %d",
                    len(candidates), fetched,
                )
