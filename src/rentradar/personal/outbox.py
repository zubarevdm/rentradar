"""Durable handoff from collection to the bot's immediate delivery lane."""

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update

from ..models import Listing
from ..storage.orm import (
    DeliveryAttemptRow,
    ListingEventRow,
    ListingRow,
    RecipientTaskRow,
    UserFilterRow,
)
from ..storage.repository import _row_to_listing
from .filters import MIN_INTERVAL_MIN, matches
from .store import PersonalStore, _row_to_filter


class Outbox:
    def __init__(self, store: PersonalStore):
        self._store = store

    async def expand(self, now: datetime, since: datetime) -> int:
        # Avoid taking a writer lock on each idle poll.
        async with self._store._session() as session:
            if await session.scalar(select(ListingEventRow.id).limit(1)) is None:
                return 0
        async with self._store._write() as session:
            events = (await session.execute(
                select(ListingEventRow, ListingRow)
                .join(ListingRow, (ListingRow.source == ListingEventRow.source)
                      & (ListingRow.external_id == ListingEventRow.external_id), isouter=True)
                .order_by(ListingEventRow.id).limit(40)
            )).all()
            filters = [_row_to_filter(row) for row in await session.scalars(
                select(UserFilterRow).where(
                    UserFilterRow.active.is_(True),
                    UserFilterRow.interval_min <= MIN_INTERVAL_MIN,
                    UserFilterRow.last_checked_at.is_not(None),
                )
            )]
            for event, row in events:
                if row is not None and event.occurred_at >= since and row.photos:
                    listing = _row_to_listing(row)
                    users = {
                        flt.user_id for flt in filters
                        if event.occurred_at > flt.last_checked_at and matches(listing, flt)
                    }
                    session.add_all(
                        RecipientTaskRow(
                            user_id=user_id, event_id=event.id, source=event.source,
                            external_id=event.external_id, created_at=event.occurred_at,
                            ready_at=now,
                        )
                        for user_id in users
                    )
                # Confirm the event only in the same commit as its recipients.
                # Scheduled and cold filters recover from their stored scan cursor.
                await session.delete(event)
            return len(events)

    async def ready(self, now: datetime) -> list[int]:
        async with self._store._session() as session:
            return list(await session.scalars(
                select(RecipientTaskRow.user_id).where(RecipientTaskRow.ready_at <= now)
                .group_by(RecipientTaskRow.user_id).order_by(func.min(RecipientTaskRow.ready_at))
            ))

    async def batch(self, user_id: int, now: datetime) -> tuple[list[int], list[Listing]]:
        async with self._store._session() as session:
            tasks = (await session.execute(
                select(RecipientTaskRow.event_id, ListingRow)
                .join(ListingRow, (ListingRow.source == RecipientTaskRow.source)
                      & (ListingRow.external_id == RecipientTaskRow.external_id), isouter=True)
                .where(RecipientTaskRow.user_id == user_id, RecipientTaskRow.ready_at <= now)
                .order_by(func.coalesce(ListingRow.published_at, ListingRow.collected_at).desc(),
                          RecipientTaskRow.event_id.desc())
                .limit(200)
            )).all()
            revisions: dict[tuple[str, str], Listing] = {}
            for _, row in tasks:
                if row is not None:
                    revisions.setdefault((row.source, row.external_id), _row_to_listing(row))
            return [event_id for event_id, _ in tasks], list(revisions.values())

    async def finish(
        self, user_id: int, event_ids: list[int], now: datetime, *, complete: bool,
    ) -> None:
        # A killed bot leaves pending tasks in SQLite. Delivery's own per-card
        # lease and journal resolve any send that was in progress at the crash.
        if not event_ids:
            return
        where = (RecipientTaskRow.user_id == user_id, RecipientTaskRow.event_id.in_(event_ids))
        async with self._store._session() as session:
            stmt = (delete(RecipientTaskRow).where(*where) if complete else
                    update(RecipientTaskRow).where(*where)
                    .values(ready_at=now + timedelta(seconds=15)))
            await session.execute(stmt)
            await session.commit()

    async def prune(self, since: datetime) -> None:
        async with self._store._session() as session:
            await session.execute(delete(ListingEventRow).where(
                ListingEventRow.occurred_at < since
            ))
            await session.execute(delete(RecipientTaskRow).where(
                RecipientTaskRow.created_at < since
            ))
            await session.commit()

    async def metrics(self, now: datetime) -> tuple[int, int, int]:
        async with self._store._session() as session:
            oldest = await session.scalar(select(func.min(RecipientTaskRow.created_at)))
            retries = await session.scalar(select(func.count()).select_from(
                DeliveryAttemptRow
            ).where(DeliveryAttemptRow.state != "sending"))
            uncertain = await session.scalar(select(func.count()).select_from(
                DeliveryAttemptRow
            ).where(DeliveryAttemptRow.state.in_(("uncertain", "sending"))))
        return (max(0, int((now - oldest).total_seconds())) if oldest else 0,
                retries or 0, uncertain or 0)
