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
    stats = {"n": len(values), "p95": values[math.ceil(len(values) * .95) - 1], "max": values[-1]}
    if len(values) < 10:
        stats["samples"] = values
    return stats


def emit(start: int, end: int) -> dict:
    journal = run("journalctl", "--since", f"@{start}", "--until", f"@{end}",
                  "--no-pager", "-o", "json", *(x for unit in UNITS for x in ("-u", unit)))
    restarts, passes, channels, errors = Counter(), Counter(), Counter(), Counter()
    http_codes, route_results = Counter(), Counter()
    search_starts, cooldowns = {}, []
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
        if match := re.search(
            r"source=(\w+) collected_to_ack_seconds=([\d.]+) lane=(\w+)", message,
        ):
            delays[match[3]].append(float(match[2]))
            delays[f"{match[1]}/{match[3]}"].append(float(match[2]))
        if match := re.search(r"avito_http run_id=(\S+) page=(\d+) started=([\d.]+) "
                              r"status=(\d+)", message):
            http_codes[f"{unit}/{match[4]}"] += 1
            if match[1] != "-" and match[2] == "1":
                search_starts.setdefault(match[1], float(match[3]))
        if match := re.search(r"avito_route .*?wait_ms=(\d+) hold_ms=(\d+) result=(\w+)", message):
            route_results[f"{unit}/{match[3]}"] += 1
            delays[f"{unit}/route_wait"].append(int(match[1]) / 1000)
            delays[f"{unit}/route_hold"].append(int(match[2]) / 1000)
        if match := re.search(r"пауза запросов \d+ секунд cooldown_until=([\d.]+)", message):
            cooldowns.append((int(row["__REALTIME_TIMESTAMP"]) / 1_000_000, float(match[1])))
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
    starts = sorted(search_starts.values())
    delays["avito/search_gap"] = [
        round(b - a, 3) for a, b in zip(starts, starts[1:], strict=False)
    ]
    cooldown_seconds = 0.0
    covered_until = float(start)
    for begins, until in sorted(cooldowns):
        until = min(until, end)
        cooldown_seconds += max(0, until - max(begins, covered_until))
        covered_until = max(covered_until, until)
    with closing(sqlite3.connect(DB, uri=True, timeout=3)) as db:
        stamps = (sql_time(start), sql_time(end))
        listings = dict(db.execute("SELECT source,count(*) FROM listings WHERE created_at>=? "
                                   "AND created_at<? GROUP BY source", stamps))
        for source, delay in db.execute(
            "SELECT source, 86400 * (julianday(created_at) - julianday(published_at)) "
            "FROM listings WHERE created_at>=? AND created_at<? AND published_at<=created_at",
            stamps,
        ):
            if delay is not None:
                delays[f"{source}/discovery"].append(round(delay, 3))
        sent, recipients = db.execute(
            "SELECT count(*),count(DISTINCT user_id) FROM user_sent "
            "WHERE delivered=1 AND sent_at>=? AND sent_at<?", stamps,
        ).fetchone()
        public = db.execute("SELECT count(*) FROM posted_log WHERE posted_at>=? AND posted_at<?",
                            stamps).fetchone()[0]
        queue = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in
                 ("listing_events", "recipient_tasks", "delivery_attempts", "avito_detail_tasks")}
        detail_queue = dict(db.execute("SELECT reason,count(*) FROM avito_detail_tasks "
                                       "GROUP BY reason"))
        oldest_ready = db.execute(
            "SELECT round(86400 * (julianday('now') - julianday(min(created_at)))) "
            "FROM avito_detail_tasks WHERE ready_at<=CURRENT_TIMESTAMP AND attempts<5"
        ).fetchone()[0]
        sqlite_check = db.execute("PRAGMA quick_check").fetchone()[0]
    status = {unit: run("systemctl", "show", "-p", "ActiveState", "--value", unit)
              for unit in UNITS}
    return {"start_msk": datetime.fromtimestamp(start, MSK).isoformat(),
            "end_msk": datetime.fromtimestamp(end, MSK).isoformat(),
            "status_at_sample": status, "listings": listings, "sent": sent,
            "recipients": recipients,
            "public": public, "queue_at_sample": queue, "sqlite": sqlite_check,
            "detail_queue_at_sample": detail_queue, "detail_oldest_ready_seconds": oldest_ready,
            "restarts": restarts, "passes": passes, "photos": channels, "errors": errors,
            "avito_search_starts": len(starts), "avito_http_codes": http_codes,
            "avito_route_results": route_results,
            "avito_cooldown_recorded_seconds": round(cooldown_seconds, 3),
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
