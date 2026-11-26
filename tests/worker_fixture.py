"""Deterministic external services for the process and systemd acceptance tests."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

from sqlalchemy import event
from sqlalchemy.orm import Session

from rentradar.config import SearchProfile, Settings
from rentradar.errors import CollectorBlockedError
from rentradar.interfaces import BaseCollector
from rentradar.models import DealType, Listing, PropertyType, RawListing, Source
from rentradar.personal.bot import build_router
from rentradar.personal.dispatcher import PersonalDispatcher
from rentradar.personal.outbox import Outbox
from rentradar.personal.store import PersonalStore
from rentradar.publisher import TelegramPublisher
from rentradar.runtime import notify_systemd, owner, run_until_signal
from rentradar.scoring import PlaceholderScoringEngine
from rentradar.storage import SqlStorage
from rentradar.worker import run_worker, utcnow


def record(path: Path, *values: object) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(values) + "\n")


def make_listing(source: str, label: str, **updates) -> Listing:
    data = {
        "source": Source(source), "external_id": label, "url": f"https://example.test/{label}",
        "deal_type": DealType.LONG_RENT, "property_type": PropertyType.FLAT,
        "price": 55_000, "area": 38, "rooms": 1, "city": "Москва",
        "address": "Тестовая, " + label, "collected_at": utcnow(),
        "photos": [f"https://example.test/{label}.jpg"],
    }
    return Listing(**(data | updates))


class Feed(BaseCollector):
    def __init__(self, folder: Path, source: str):
        self.folder = folder
        self.source_name = source

    async def fetch(self, profile: SearchProfile, *, limit: int) -> list[RawListing]:
        return [raw async for page in self.fetch_pages(profile, limit=limit) for raw in page]

    async def fetch_pages(self, profile: SearchProfile, *, limit: int):
        path = self.folder / f"{self.source_name}.input"
        if not path.exists():
            return
        label = path.read_text(encoding="utf-8")
        path.unlink()
        record(self.folder / "fixture.log", "fetch", self.source_name, label, os.getpid())
        if label == "async-hang":
            await asyncio.Event().wait()
        if label == "loop-hang":
            time.sleep(60)
        partial = label.startswith("partial")
        yield [
            RawListing(source=Source(self.source_name), external_id=item,
                       url=f"https://example.test/{item}")
            for item in ([label, "bad"] if partial else [label])
        ]
        if partial:
            raise CollectorBlockedError("second page")

    def normalize(self, raw: RawListing):
        if raw.external_id == "bad":
            raise ValueError("malformed listing")
        return make_listing(self.source_name, raw.external_id)


class Telegram:
    def __init__(self, folder: Path):
        self.folder = folder

    async def send_photo(self, *, chat_id, photo, **kwargs):
        label = str(photo).split("/")[-1].removesuffix(".jpg")
        record(self.folder / "fixture.log", "photo", str(chat_id), label, os.getpid())
        if (self.folder / f"stall-{chat_id}").exists():
            await asyncio.Event().wait()

    async def send_message(self, **kwargs):
        raise AssertionError("Expected a media message")


async def telegram_role(folder: Path, settings: Settings, storage: SqlStorage) -> None:
    store = PersonalStore(storage)
    publisher = TelegramPublisher(dry_run=False, bot=Telegram(folder))
    publisher._download_photos = AsyncMock(side_effect=lambda urls, **_: urls)
    dispatcher = PersonalDispatcher(
        store=store, storage=storage, settings=settings, scoring=PlaceholderScoringEngine(),
        publisher=publisher,
    )
    router = build_router(store, settings)
    menu = next(handler.callback for handler in router.message.handlers
                if handler.callback.__name__ == "show_menu")
    record(folder / "fixture.log", "bot", os.getpid())
    notify_systemd("READY=1")
    menu_path = folder / "menu.input"
    try:
        while True:
            await dispatcher.poll_outbox(utcnow())
            await dispatcher.queue_due(utcnow())
            if menu_path.exists():
                menu_path.unlink()
                message = AsyncMock()
                message.from_user.id = 1001
                message.from_user.username = "fixture"
                message.date = datetime.now(UTC)
                await menu(message, AsyncMock())
                assert message.answer.await_count == 1
                record(folder / "fixture.log", "menu", os.getpid())
            notify_systemd("WATCHDOG=1")
            await asyncio.sleep(0.1)
    finally:
        await dispatcher.close()


async def main(folder: Path, role: str) -> None:
    path = folder / "shared.db"
    settings = Settings(
        _env_file=None, db_path=path, dry_run=False, avito_detail_max_per_run=0,
        worker_pass_timeout_sec=2,
    )
    storage = SqlStorage(settings.db_url)
    try:
        with owner(path, role):
            if role in ("before-commit", "before-fanout"):
                def crash(session):
                    session.flush()
                    os._exit(21)

                event.listen(Session, "before_commit", crash)
                if role == "before-commit":
                    await storage.upsert_listings([make_listing("cian", "lost")])
                else:
                    now = utcnow()
                    await Outbox(PersonalStore(storage)).expand(now, datetime.min)
            if role == "after-commit":
                await storage.upsert_listings([make_listing("cian", "committed")])
                os._exit(22)
            if role == "bot":
                await run_until_signal(telegram_role(folder, settings, storage))
            else:
                await run_until_signal(run_worker(
                    settings, role, [SearchProfile(name="fixture", city="Москва")], storage,
                    collector=Feed(folder, role),
                ))
    finally:
        await storage.dispose()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
