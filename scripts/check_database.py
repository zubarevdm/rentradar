"""Create a protected SQLite snapshot and verify it, optionally migrating twice."""

import argparse
import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

from rentradar.config import Settings
from rentradar.storage import SqlStorage

TABLES = (
    "listings", "user_filters", "subscribers", "user_sent",
    "delivery_attempts", "manual_payments", "posted_log",
)


def counts(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("SQLite quick_check failed")
        existing = {row[0] for row in db.execute("SELECT name FROM sqlite_schema")}
        return {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in TABLES if name in existing}


async def migrate(path: Path, pause_max_days: int) -> None:
    storage = SqlStorage(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        await storage.init(pause_max_days=pause_max_days)
        await storage.init(pause_max_days=pause_max_days)
    finally:
        await storage.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--test-migration", action="store_true")
    args = parser.parse_args()
    snapshot = args.snapshot.resolve()
    settings = Settings()
    if snapshot.exists():
        raise FileExistsError(snapshot)
    source = settings.db_path.resolve().as_uri() + "?mode=ro"
    with (
        closing(sqlite3.connect(source, uri=True)) as src,
        closing(sqlite3.connect(snapshot)) as dst,
    ):
        src.backup(dst)
    before = counts(snapshot)
    if args.test_migration:
        asyncio.run(migrate(snapshot, settings.pause_max_days))
        after = counts(snapshot)
        if any(after.get(table) != count for table, count in before.items()):
            raise RuntimeError(f"Migration changed counts: before={before}, after={after}")
    print(f"SQLITE_OK migration_twice={args.test_migration} counts={before}")


if __name__ == "__main__":
    main()
