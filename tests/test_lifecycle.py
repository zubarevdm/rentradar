"""Тесты выбора lifecycle-напоминания (_lifecycle_nudge). Чистая логика, без сети."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from rentradar.cli import _lifecycle_nudge
from rentradar.config import Settings
from rentradar.personal.access import add_hours, plural
from rentradar.storage.orm import SubscriberRow

NOW = datetime(2026, 6, 21, 12, 0, 0)
S = Settings(lifecycle_expiry_days=3, lifecycle_winback_days=7, lifecycle_trial_soon_hours=4)


def _sub(**kw: object) -> SubscriberRow:
    base: dict = {
        "telegram_id": 1,
        "free_sends_used": 0,
        "paid_until": None,
        "paused_at": None,
        "nudged_trial": False,
        "nudged_trial_soon": False,
        "nudged_expiry": False,
        "nudged_winback": False,
    }
    base.update(kw)
    return SubscriberRow(**base)


def test_expiry_soon() -> None:
    kind, text = _lifecycle_nudge(_sub(paid_until=NOW + timedelta(days=2)), NOW, S)
    assert kind == "expiry"
    assert "23.06 в 15:00 МСК" in text


def test_active_far_from_expiry_silent() -> None:
    assert _lifecycle_nudge(_sub(paid_until=NOW + timedelta(days=20)), NOW, S)[0] is None


@pytest.mark.parametrize("hours,silent_after,nudge_after", [
    (24, 2, 21), (72, 24, 62), (168, 120, 143), (720, 600, 650),
])
def test_short_tariff_is_not_asked_to_renew_right_after_purchase(
    hours: int, silent_after: int, nudge_after: int
) -> None:
    sub = _sub()
    add_hours(sub, NOW, hours, 2)
    assert _lifecycle_nudge(sub, NOW + timedelta(hours=silent_after), S)[0] is None
    assert _lifecycle_nudge(sub, NOW + timedelta(hours=nudge_after), S)[0] == "expiry"


def test_winback_after_expiry() -> None:
    assert _lifecycle_nudge(_sub(paid_until=NOW - timedelta(days=2)), NOW, S)[0] == "winback"


def test_winback_window_passed() -> None:
    assert _lifecycle_nudge(_sub(paid_until=NOW - timedelta(days=30)), NOW, S)[0] is None


def test_trial_exhausted() -> None:
    assert _lifecycle_nudge(_sub(trial_until=NOW), NOW, S)[0] == "trial"


def test_trial_end_is_one_message_not_two() -> None:
    sub = _sub(trial_until=NOW - timedelta(hours=1), nudged_trial=True)
    assert _lifecycle_nudge(sub, NOW, S)[0] is None


def test_trial_far_from_end_silent() -> None:
    assert _lifecycle_nudge(_sub(trial_until=NOW + timedelta(hours=5)), NOW, S)[0] is None


def test_trial_soon_counts_delivered_and_offers_cheapest_tariff() -> None:
    sub = _sub(trial_until=NOW + timedelta(hours=3))
    kind, text = _lifecycle_nudge(sub, NOW, S, delivered=14)
    assert kind == "trial_soon"
    assert "сегодня в 18:00 МСК" in text
    assert "прислал тебе 14 вариантов" in text and "от 49 ₽" in text
    assert _lifecycle_nudge(_sub(trial_until=NOW + timedelta(hours=3), nudged_trial_soon=True),
                            NOW, S)[0] is None


def test_trial_soon_without_filters_asks_to_set_up_search() -> None:
    sub = _sub(trial_until=NOW + timedelta(hours=2))
    kind, text = _lifecycle_nudge(sub, NOW, S, has_filters=False)
    assert kind == "trial_soon" and "поиск ещё не настроен" in text


def test_paid_after_trial_is_not_nudged_as_trial() -> None:
    sub = _sub(trial_until=NOW + timedelta(hours=2))
    add_hours(sub, NOW, 72, 2)  # bought during the trial: paid time starts after it
    assert _lifecycle_nudge(sub, NOW, S)[0] is None


def test_paused_never_nudged() -> None:
    sub = _sub(paid_until=NOW + timedelta(days=2), paused_at=NOW)
    assert _lifecycle_nudge(sub, NOW, S)[0] is None


def test_dedup_already_nudged() -> None:
    sub = _sub(paid_until=NOW + timedelta(days=2), nudged_expiry=True)
    assert _lifecycle_nudge(sub, NOW, S)[0] is None


@pytest.mark.parametrize("n,word", [
    (1, "вариант"), (3, "варианта"), (5, "вариантов"), (11, "вариантов"),
    (21, "вариант"), (22, "варианта"), (112, "вариантов"),
])
def test_plural(n: int, word: str) -> None:
    assert plural(n, "вариант", "варианта", "вариантов") == word
