"""Ownership and resource limits shared by processes using the same database."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import signal
import socket
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .errors import CollectionSkipped

log = logging.getLogger(__name__)
_database: Path | None = None


def configure_database(path: Path) -> None:
    global _database
    _database = path.resolve()


@contextmanager
def _try_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as fd:
        if os.name == "nt":
            import msvcrt

            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno != 13:
                    raise
                yield False
                return
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        yield True


def _lock_path(path: Path, name: str) -> Path:
    return path.with_name(path.name + ".locks") / name


@contextmanager
def owner(path: Path, role: str) -> Iterator[None]:
    with _try_lock(_lock_path(path.resolve(), role)) as held:
        if not held:
            raise RuntimeError(f"{role} already owns this database")
        yield


@asynccontextmanager
async def mobile_request(proxy: str | None) -> AsyncIterator[None]:
    if not proxy or _database is None:
        yield
        return
    endpoint = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    identity = f"{endpoint.hostname}:{endpoint.port}"
    key = hashlib.sha256(identity.encode()).hexdigest()[:24]
    path = _lock_path(_database, f"proxy-{key}")
    deadline = asyncio.get_running_loop().time() + 8
    while True:
        with _try_lock(path) as held:
            if held:
                yield
                return
        if asyncio.get_running_loop().time() >= deadline:
            raise CollectionSkipped("shared proxy busy")
        await asyncio.sleep(0.25)


def notify_systemd(message: str) -> None:
    address = os.getenv("NOTIFY_SOCKET")
    if address is None:
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.sendto(message.encode(), "\0" + address[1:] if address.startswith("@") else address)


def log_start(role: str, source: str = "-") -> None:
    path = Path(__file__).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        sha = result.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        sha = "unknown"
    expected = os.getenv("RENTRADAR_EXPECTED_SHA")
    if expected and sha != expected:
        raise RuntimeError(f"Imported {path} at {sha}, expected {expected}")
    log.info("runtime_start role=%s source=%s pid=%d sha=%s path=%s",
             role, source, os.getpid(), sha, path)


async def run_until_signal(work) -> None:
    task = asyncio.create_task(work)
    loop = asyncio.get_running_loop()
    if os.name != "nt":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        log.info("Process stopped by signal")
    finally:
        if os.name != "nt":
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
