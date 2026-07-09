"""Тесты выбора lifecycle-напоминания (_lifecycle_nudge). Чистая логика, без сети."""

from __future__ import annotations

from datetime import datetime, timedelta

from rentradar.cli import _lifecycle_nudge
from rentradar.config import Settings
from rentradar.storage.orm import SubscriberRow

NOW = datetime(2026, 6, 21, 12, 0, 0)
S = Settings(free_sends_limit=5, lifecycle_expiry_days=3, lifecycle_winback_days=7)


def _sub(**kw: object) -> SubscriberRow:
    base: dict = {
        "telegram_id": 1,
        "free_sends_used": 0,
        "paid_until": None,
        "paused_at": None,
        "nudged_trial": False,
        "nudged_expiry": False,
        "nudged_winback": False,
    }
    base.update(kw)
    return SubscriberRow(**base)


def test_expiry_soon() -> None:
    kind, text = _lifecycle_nudge(_sub(paid_until=NOW + timedelta(days=2)), NOW, S)
    assert kind == "expiry"
    assert "2 дн" in text


def test_active_far_from_expiry_silent() -> None:
    assert _lifecycle_nudge(_sub(paid_until=NOW + timedelta(days=20)), NOW, S)[0] is None


def test_winback_after_expiry() -> None:
    assert _lifecycle_nudge(_sub(paid_until=NOW - timedelta(days=2)), NOW, S)[0] == "winback"


def test_winback_window_passed() -> None:
    assert _lifecycle_nudge(_sub(paid_until=NOW - timedelta(days=30)), NOW, S)[0] is None


def test_trial_exhausted() -> None:
    assert _lifecycle_nudge(_sub(free_sends_used=5), NOW, S)[0] == "trial"


def test_trial_not_exhausted_silent() -> None:
    assert _lifecycle_nudge(_sub(free_sends_used=2), NOW, S)[0] is None


def test_paused_never_nudged() -> None:
    sub = _sub(paid_until=NOW + timedelta(days=2), paused_at=NOW)
    assert _lifecycle_nudge(sub, NOW, S)[0] is None


def test_dedup_already_nudged() -> None:
    sub = _sub(paid_until=NOW + timedelta(days=2), nudged_expiry=True)
    assert _lifecycle_nudge(sub, NOW, S)[0] is None
