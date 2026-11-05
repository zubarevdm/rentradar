import asyncio
from datetime import timedelta
from sqlalchemy import select, func
from rentradar.storage.orm import UserSentRow
from tests.test_navigation_metro_fixes import env, NOW

async def test_repeated_and_concurrent_sent_writes_preserve_delivery(env):
    store, _, _ = env
    await asyncio.gather(*(store.mark_sent(1, 'same', delivered=True, now=NOW) for _ in range(4)))
    await store.mark_sent(1, 'same', delivered=False, now=NOW + timedelta(hours=1))
    async with store._session() as session:
        assert await session.scalar(select(func.count()).select_from(UserSentRow)) == 1
        row = await session.scalar(select(UserSentRow))
        assert row.delivered is True and row.sent_at == NOW
    assert await store.count_delivered(1, NOW - timedelta(minutes=1)) == 1

async def test_skipped_row_can_be_promoted_to_delivered(env):
    store, _, _ = env
    await store.mark_sent(1, 'same', now=NOW)
    later = NOW + timedelta(hours=1)
    await store.mark_sent(1, 'same', delivered=True, now=later)
    async with store._session() as session:
        row = await session.scalar(select(UserSentRow))
        assert row.delivered is True and row.sent_at == later
