"""Small shared access policy. Dates in the database are naive UTC."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..storage.orm import SubscriberRow


@dataclass(frozen=True)
class Tariff:
    code: str
    hours: int
    price: int
    label: str

    @property
    def duration(self) -> str:
        days = self.hours // 24
        return f"{days} " + ("день" if days == 1 else "дня" if days == 3 else "дней")

    @property
    def title(self) -> str:
        return self.duration if self.label == self.duration else f"{self.label} · {self.duration}"


TARIFFS = {
    t.code: t
    for t in (
        Tariff("24h", 24, 49, "Попробовать сервис"),
        Tariff("3d", 72, 129, "3 дня"),
        Tariff("7d", 168, 249, "7 дней"),
        Tariff("30d", 720, 590, "Выгоднее всего"),
    )
}


TRIAL = timedelta(hours=24)
_MSK = timedelta(hours=3)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def moscow(value: datetime) -> str:
    return (value + _MSK).strftime("%d.%m.%Y %H:%M МСК")


def moscow_when(value: datetime, now: datetime) -> str:
    """«сегодня в 18:40 МСК» / «завтра в 09:15 МСК» / «31.10 в 12:00 МСК»."""
    local, today = value + _MSK, (now + _MSK).date()
    day = {today: "сегодня", today + timedelta(days=1): "завтра"}.get(
        local.date(), local.strftime("%d.%m")
    )
    return f"{day} в {local:%H:%M} МСК"


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу: 1 вариант, 3 варианта, 5 вариантов."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


@dataclass(frozen=True)
class Access:
    until: datetime | None
    trial_until: datetime | None
    paused: bool
    frozen_seconds: float = 0

    def active(self, now: datetime) -> bool:
        return self.until is not None and self.until > now


def access_of(row: SubscriberRow, now: datetime, pause_max_days: int = 2) -> Access:
    trial = row.trial_until
    pu = row.paid_until
    paused = row.paused_at is not None and now < row.paused_at + timedelta(days=pause_max_days)
    frozen = 0.0
    if row.paused_at is not None and pu is not None:
        if paused:
            frozen = max(0, (pu - row.paused_at).total_seconds())
            pu = None
        else:
            pu += timedelta(days=pause_max_days)
    ends = [v for v in (trial, pu) if v is not None]
    return Access(max(ends) if ends else None, trial, paused, frozen)


def normalize_pause(row: SubscriberRow, now: datetime, pause_max_days: int) -> None:
    if row.paused_at is not None and now >= row.paused_at + timedelta(days=pause_max_days):
        if row.paid_until is not None:
            row.paid_until += timedelta(days=pause_max_days)
        row.paused_at = None


def add_hours(row: SubscriberRow, now: datetime, hours: float, pause_max_days: int) -> None:
    normalize_pause(row, now, pause_max_days)
    if row.paused_at is not None:
        base = max(row.paused_at, row.paid_until or row.paused_at)
    else:
        base = max(now, row.paid_until or now, row.trial_until or now)
        if row.paid_until is None or row.paid_until <= now:
            row.access_starts_at = max(now, row.trial_until or now)
    row.paid_until = base + timedelta(hours=hours)
    row.nudged_expiry = False
    row.nudged_winback = False
