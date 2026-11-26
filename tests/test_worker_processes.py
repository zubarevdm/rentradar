"""Exercise real process boundaries using one temporary SQLite file."""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from rentradar.personal import PersonalStore, UserFilter
from rentradar.personal.outbox import Outbox
from rentradar.storage import SqlStorage
from rentradar.storage.orm import (
    DeliveryAttemptRow,
    ListingEventRow,
    RecipientTaskRow,
    UserFilterRow,
)
from rentradar.worker import WorkerState, utcnow
from tests.worker_fixture import make_listing

SYSTEMD = os.getenv("RENTRADAR_TEST_SYSTEMD") == "1"
REPO = Path(__file__).resolve().parents[1]


class Services:
    def __init__(self, folder: Path):
        self.folder = folder
        self.processes = {}
        self.prefix = f"rentradar-split-stage-{os.getpid()}-{os.urandom(3).hex()}"

    def start(self, role: str):
        cmd = [sys.executable, "-m", "tests.worker_fixture", str(self.folder), role]
        if SYSTEMD and role in ("bot", "avito", "cian", "yandex"):
            name = f"{self.prefix}-{role}"
            subprocess.run([
                "systemd-run", "--quiet", "--collect", f"--unit={name}",
                f"--working-directory={REPO}", f"--setenv=PYTHONPATH={REPO / 'src'}",
                "--property=Type=notify", "--property=NotifyAccess=main",
                "--property=Restart=on-failure", "--property=RestartSec=1s",
                "--property=WatchdogSec=8s", "--property=TimeoutStopSec=4s", *cmd,
            ], check=True)
            self.processes[role] = name
        else:
            log = self.folder / f"{role}.log"
            with log.open("ab") as stream:
                self.processes[role] = subprocess.Popen(
                    cmd, cwd=REPO, stdout=stream, stderr=subprocess.STDOUT,  # noqa: SIM115
                )
        return self.processes[role]

    def pid(self, role: str) -> int:
        process = self.processes[role]
        if isinstance(process, str):
            result = subprocess.run(
                ["systemctl", "show", "-p", "MainPID", "--value", process],
                text=True, capture_output=True, check=False,
            )
            return int(result.stdout.strip() or "0")
        return process.pid if process.poll() is None else 0

    def stop(self, role: str) -> None:
        process = self.processes.pop(role)
        if isinstance(process, str):
            subprocess.run(["systemctl", "stop", process], check=True)
        elif process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def crash(self, role: str) -> None:
        process = self.processes[role]
        if isinstance(process, str):
            subprocess.run([
                "systemctl", "kill", "--signal=KILL", "--kill-whom=main", process,
            ], check=True)
        else:
            process.kill()
            process.wait(timeout=5)

    def submit(self, role: str, text: str) -> None:
        pending = self.folder / f"{role}.tmp"
        pending.write_text(text, encoding="utf-8")
        pending.replace(self.folder / f"{role}.input")

    def records(self) -> list:
        log = self.folder / "fixture.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def diagnostics(self) -> dict:
        result = {p.name: p.read_text(encoding="utf-8", errors="replace")[-1500:]
                  for p in self.folder.glob("*.log")}
        for role, process in self.processes.items():
            if isinstance(process, str):
                log = subprocess.run(
                    ["journalctl", "-u", process, "--no-pager", "-o", "cat", "-n", "15"],
                    capture_output=True, text=True, check=False,
                )
                result[role] = log.stdout
        return result


@pytest.fixture
async def shared(tmp_path):
    storage = SqlStorage(f"sqlite+aiosqlite:///{(tmp_path / 'shared.db').as_posix()}")
    await storage.init()
    services = Services(tmp_path)
    try:
        yield storage, services
    finally:
        for role in list(reversed(services.processes)):
            services.stop(role)
        await storage.dispose()


async def wait_for(services, predicate, timeout=18):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.1)
    pytest.fail(str(services.diagnostics()))


async def wait_sync(services, predicate, timeout=18):
    async def check():
        return predicate()
    await wait_for(services, check, timeout)


async def subscribe(storage, user: int, *, minutes: int = 5, price_max: int = 60_000):
    store = PersonalStore(storage)
    now = utcnow()
    await store.get_or_create_subscriber(user)
    await store.grant_hours(user, now, 2)
    filter_id = await store.upsert_filter(UserFilter(
        user_id=user, city="Москва", interval_min=minutes, price_max=price_max,
        include_backlog=False,
    ))
    await store.touch_filter(filter_id, now - timedelta(minutes=1))
    return filter_id


async def request(states, source, services):
    async def ask():
        return await states.request(source, utcnow()) in ("queued", "requested")
    await wait_for(services, ask)


def received(services, user, label):
    return sum(row[:3] == ["photo", str(user), label] for row in services.records())


async def test_source_stop_watchdog_and_recovery(shared):
    storage, services = shared
    await subscribe(storage, 1001)
    states = WorkerState(storage)
    for role in ("bot", "avito", "cian", "yandex"):
        services.start(role)
    avito_pid = services.pid("avito")
    bot_pid = services.pid("bot")
    await wait_sync(services, lambda: any(row[0] == "bot" for row in services.records()))
    polling_pid = next(row[1] for row in services.records() if row[0] == "bot")
    services.stop("avito")
    for role in ("cian", "yandex"):
        services.submit(role, f"initial-{role}")
        await request(states, role, services)
    services.submit("menu", "open")
    await wait_sync(services, lambda: received(services, 1001, "initial-cian") == 1)
    await wait_sync(services, lambda: ["menu", polling_pid] in services.records())
    assert services.pid("bot") == bot_pid

    async def yandex_ok():
        row = (await states.read()).get("yandex")
        return row is not None and row.state == "ok" and row.new_count == 1

    await wait_for(services, yandex_ok)
    services.start("avito")
    assert services.pid("avito") != avito_pid
    hung_pid = services.pid("avito")
    services.submit("avito", "loop-hang" if SYSTEMD else "async-hang")
    await request(states, "avito", services)
    await wait_sync(services, lambda: any(row[:2] == ["fetch", "avito"]
                                         for row in services.records()))
    assert await states.request("avito", utcnow()) == "running"
    services.submit("cian", "partial-cian")
    services.submit("yandex", "second-yandex")
    services.submit("menu", "open")
    await request(states, "cian", services)
    await request(states, "yandex", services)
    await wait_sync(services, lambda: received(services, 1001, "partial-cian") == 1)
    await wait_sync(services, lambda: received(services, 1001, "second-yandex") == 1)
    await wait_sync(services, lambda: services.records().count(["menu", polling_pid]) == 2)
    assert (await states.read())["cian"].state == "partial"

    if SYSTEMD:
        await wait_sync(
            services, lambda: services.pid("avito") not in (0, hung_pid), timeout=25,
        )
    else:
        await wait_sync(services, lambda: services.pid("avito") == 0)
        assert (await states.read())["avito"].state == "timeout"
        services.stop("avito")
        services.start("avito")
    services.submit("avito", "avito-return")
    await request(states, "avito", services)
    async def avito_resumed():
        return await storage.get_listing("avito", "avito-return") is not None
    await wait_for(services, avito_resumed)
    assert services.pid("bot") == bot_pid
    assert received(services, 1001, "initial-cian") == 1
    assert received(services, 1001, "partial-cian") == 1


async def test_commit_fanout_and_recipient_crash(shared):
    storage, services = shared
    await subscribe(storage, 1001)
    await subscribe(storage, 1002)
    before = services.start("before-commit")
    assert before.wait(timeout=15) == 21
    after = services.start("after-commit")
    assert after.wait(timeout=15) == 22
    assert await storage.get_listing("cian", "lost") is None
    async with storage._session() as session:
        events = list(await session.scalars(select(ListingEventRow.external_id)))
    assert events == ["committed"]
    outbox = Outbox(PersonalStore(storage))
    now = utcnow()
    fanout = services.start("before-fanout")
    assert fanout.wait(timeout=15) == 21
    assert await outbox.ready(now) == []
    assert await outbox.expand(now, now - timedelta(days=1)) == 1
    assert await outbox.ready(now) == [1001, 1002]

    (services.folder / "stall-1002").touch()
    services.start("bot")
    await wait_sync(services, lambda: received(services, 1002, "committed") == 1)

    async def fast_confirmed():
        return await PersonalStore(storage).count_delivered(1001, now) == 1

    await wait_for(services, fast_confirmed)
    await storage.upsert_listings([make_listing("cian", "second-while-stalled")])
    await wait_sync(services, lambda: received(services, 1001, "second-while-stalled") == 1)
    bot_pid = services.pid("bot")
    services.crash("bot")
    (services.folder / "stall-1002").unlink()
    if SYSTEMD:
        await wait_sync(services, lambda: services.pid("bot") not in (0, bot_pid))
    else:
        services.stop("bot")
        services.start("bot")
    await wait_sync(services, lambda: received(services, 1002, "second-while-stalled") == 1)
    assert received(services, 1001, "committed") == 1
    assert received(services, 1002, "committed") == 1
    async with storage._session() as session:
        assert await session.scalar(select(func.count()).select_from(RecipientTaskRow)) >= 1
        assert await session.scalar(select(DeliveryAttemptRow.state).where(
            DeliveryAttemptRow.user_id == 1002,
            DeliveryAttemptRow.state == "sending",
        )) == "sending"  # A crash before ACK leaves the deliberate 15-minute lease.


async def test_revisions_and_scheduled_interval(shared):
    storage, services = shared
    await subscribe(storage, 1001)
    await subscribe(storage, 1002, minutes=15)
    now = utcnow()
    listing = make_listing("cian", "late", price=70_000, photos=[])
    await asyncio.gather(
        storage.upsert_listings([listing]),
        storage.upsert_listings([make_listing("yandex", "outside", price=90_000)]),
        storage.upsert_listings([make_listing("avito", "outside", price=85_000)]),
    )
    changed = listing.model_copy(update={
        "price": 55_000, "photos": ["https://example.test/late.jpg"], "collected_at": utcnow(),
    })
    await storage.upsert_listings([changed])
    await storage.upsert_listings([changed])
    async with storage._session() as session:
        assert await session.scalar(select(func.count()).select_from(ListingEventRow)) == 4
    await storage.init()
    services.start("bot")
    await wait_sync(services, lambda: received(services, 1001, "late") == 1)
    assert received(services, 1002, "late") == 0
    store = PersonalStore(storage)
    filter_id, _ = (await store.list_filters(1002))[0]
    async with storage._session.begin() as session:
        await session.execute(update(UserFilterRow).where(
            UserFilterRow.id == filter_id
        ).values(last_checked_at=now - timedelta(minutes=15)))
    await wait_sync(services, lambda: received(services, 1002, "late") == 1)
    found = await storage.get_listing("cian", "late")
    assert found.price == 55_000 and found.first_seen_at <= found.collected_at
