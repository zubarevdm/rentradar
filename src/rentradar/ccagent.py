"""Оркестрация админ-команды /cc: запуск Claude Code в изолированном worktree.

Тонкая обёртка над scripts/cc_run.sh и scripts/cc_apply.sh: бот вызывает их как
подпроцесс и парсит одну JSON-строку из stdout. Вся «острая» логика (git-worktree,
токен, тесты, вливание в main) — в bash-скриптах на сервере, здесь только транспорт.
Ревью-модель: cc_run готовит ветку и отчёт, cc_apply вливает её на прод по команде.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


def _repo_root() -> Path:
    # src/rentradar/ccagent.py → корень репозитория на два уровня выше src.
    return Path(__file__).resolve().parents[2]


def _script(name: str) -> Path:
    return _repo_root() / "scripts" / name


def is_configured(claude_token: str) -> bool:
    """/cc доступен только когда есть токен, скрипты и это Linux-сервер (не Windows-dev)."""
    return bool(claude_token) and sys.platform.startswith("linux") and _script("cc_run.sh").exists()


async def _run(name: str, *args: str, timeout: float) -> dict:
    """Запустить bash-скрипт, вернуть распарсенный JSON последней строки stdout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            str(_script(name)),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return {"status": "error", "msg": f"не запустить {name}: {exc}"}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return {"status": "timeout"}
    text = out.decode(errors="replace").strip()
    last = text.splitlines()[-1] if text else ""
    try:
        return json.loads(last)
    except (json.JSONDecodeError, ValueError):
        tail = err.decode(errors="replace")[-300:] or text[-300:] or "нет вывода"
        return {"status": "error", "msg": tail}


async def run_task(task: str, *, timeout: float = 1300) -> dict:
    """Прогнать задачу через Claude Code. Возможные status: done / no_changes /
    busy / timeout / error. При done — branch, summary, diffstat, changed,
    pytest_rc, pytest, cost."""
    return await _run("cc_run.sh", task, timeout=timeout)


async def apply() -> dict:
    """Влить ожидающую cc-ветку в main. status: applied / nothing / conflict / error.
    Перезапуск сервиса — на стороне бота, после подтверждения."""
    return await _run("cc_apply.sh", timeout=120)
