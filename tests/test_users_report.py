from datetime import UTC, datetime

import pytest

from rentradar.personal.store import PersonalStore
from rentradar.storage.repository import SqlStorage


@pytest.mark.asyncio
async def test_users_report_counts_starts(tmp_path):
    storage = SqlStorage(f"sqlite+aiosqlite:///{(tmp_path / 'u.db').as_posix()}")
    await storage.init()
    ps = PersonalStore(storage)
    try:
        await ps.get_or_create_subscriber(1, "alice")
        await ps.get_or_create_subscriber(2, None, referred_by=1)
        now = datetime.now(UTC).replace(tzinfo=None)
        r = await ps.users_report(now)
        assert r["total"] == 2 and r["d7"] == 2 and r["d30"] == 2
        assert r["with_filter"] == 0
        assert {row[0] for row in r["recent"]} == {1, 2}
        assert any(row[1] == "alice" for row in r["recent"])
    finally:
        await storage.dispose()
