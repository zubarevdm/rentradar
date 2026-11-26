"""Read-only rollout checks from systemd, SQLite and the service journals."""

import argparse
import json
import math
import re
import sqlite3
import subprocess
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone

UNITS = (
    "rentradar.service", "rentradar-collector@avito.service", "rentradar-collector@cian.service",
    "rentradar-collector@yandex.service", "rentradar-avito-detail.service",
)
DB = "file:/opt/rentradar/data/rentradar.db?mode=ro"
MSK = timezone(timedelta(hours=3))


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True, timeout=45).strip()


def sql_time(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M:%S")


def latency(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    values.sort()
    return {"n": len(values), "p95": values[math.ceil(len(values) * .95) - 1], "max": values[-1]}


def emit(start: int, end: int) -> dict:
    journal = run("journalctl", "--since", f"@{start}", "--until", f"@{end}",
                  "--no-pager", "-o", "json", *(x for unit in UNITS for x in ("-u", unit)))
    restarts, passes, channels, errors = Counter(), Counter(), Counter(), Counter()
    delays = defaultdict(list)
    for line in journal.splitlines():
        row = json.loads(line)
        if int(row.get("__REALTIME_TIMESTAMP", 0)) >= end * 1_000_000:
            continue
        unit = next((row[k] for k in ("UNIT", "OBJECT_SYSTEMD_UNIT", "_SYSTEMD_UNIT")
                     if row.get(k) in UNITS), None)
        message = row.get("MESSAGE", "")
        if not isinstance(message, str):
            continue
        if unit and "Scheduled restart job" in message:
            restarts[unit] += 1
        if match := re.search(r"collection source=(\w+) state=(\w+)", message):
            passes["/".join(match.groups())] += 1
        if match := re.search(r"collected_to_ack_seconds=([\d.]+) lane=(\w+)", message):
            delays[match[2]].append(float(match[1]))
        if match := re.search(r"interval_min=15 lateness_seconds=([\d.]+)", message):
            delays["filter_15_lateness"].append(float(match[1]))
        if match := re.search(r"media delivered: source=(\w+) mode=([\w-]+) count=(\d+)", message):
            channels["/".join(match.groups()[:2])] += int(match[3])
        for name, marker in (
            ("avito_rate_limit", "HTTP 429"), ("avito_forbidden", "HTTP 403"),
            ("yandex_challenge", "SmartCaptcha"), ("sqlite_lock", "database is locked"),
            ("polling_failure", "Failed to fetch updates"),
            ("polling_conflict", "TelegramConflictError"),
            ("detail_deferral", "avito detail stage=parse result=deferred"),
            ("detail_cycle_timeout", "avito detail result=timeout"),
            ("send_retry", "delivery_deferred"),
        ):
            errors[name] += marker in message
    with closing(sqlite3.connect(DB, uri=True, timeout=3)) as db:
        stamps = (sql_time(start), sql_time(end))
        listings = dict(db.execute("SELECT source,count(*) FROM listings WHERE created_at>=? "
                                   "AND created_at<? GROUP BY source", stamps))
        sent, recipients = db.execute(
            "SELECT count(*),count(DISTINCT user_id) FROM user_sent "
            "WHERE delivered=1 AND sent_at>=? AND sent_at<?", stamps,
        ).fetchone()
        public = db.execute("SELECT count(*) FROM posted_log WHERE posted_at>=? AND posted_at<?",
                            stamps).fetchone()[0]
        queue = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in
                 ("listing_events", "recipient_tasks", "delivery_attempts", "avito_detail_tasks")}
        sqlite_check = db.execute("PRAGMA quick_check").fetchone()[0]
    status = {unit: run("systemctl", "show", "-p", "ActiveState", "--value", unit)
              for unit in UNITS}
    return {"start_msk": datetime.fromtimestamp(start, MSK).isoformat(),
            "end_msk": datetime.fromtimestamp(end, MSK).isoformat(),
            "status_at_sample": status, "listings": listings, "sent": sent,
            "recipients": recipients,
            "public": public, "queue_at_sample": queue, "sqlite": sqlite_check,
            "restarts": restarts, "passes": passes, "photos": channels, "errors": errors,
            "latency_seconds": {key: latency(values) for key, values in delays.items()}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since-epoch", type=int, required=True)
    parser.add_argument("--seconds", type=int, nargs="+", default=[7200, 86400])
    args = parser.parse_args()
    for seconds in sorted(set(args.seconds)):
        if seconds <= 0:
            parser.error("observation windows must be positive")
        end = args.since_epoch + seconds
        while (remaining := end - time.time()) > 0:
            time.sleep(min(60, remaining))
        print(json.dumps(emit(args.since_epoch, end)), flush=True)


if __name__ == "__main__":
    main()
