# RentRadar on systemd

One `rentradar.service` owns Telegram polling, menu callbacks, subscriptions,
matching, personal and public delivery, Vision, market statistics, reminders
and queue cleanup. `rentradar-collector@{avito,cian,yandex}.service` each own
one adapter, its interval and its journal. The Avito collector owns search and
phone checks. `rentradar-avito-detail.service` owns queued detail enrichment.
The Telegram publisher uses saved terms
and photo links. Public Avito listings without photos wait for a later slot.

All roles use `/opt/rentradar/data/rentradar.db` in WAL mode and read local
settings from `/opt/rentradar`. Releases are Git checkouts in
`/opt/rentradar-releases/<sha>`. The current symlink selects the code, while
the virtualenv, profiles, browser state and backup timer stay in place. Linux
locks exclude a second bot or collector on the same SQLite file. The sources
on this VPS share one mobile proxy. Workers lock by proxy endpoint only
while using that route. Direct requests and normal bot work keep running.

Each committed page creates events for new or changed listings. Migration
starts the event stream at the first subsequent write and creates zero
historical events. The earliest discovery remains in `created_at`. The
event ID identifies the committed revision. Matching uses the current saved
listing, so a newer revision supersedes stale content, and its own event stays
eligible for processing. The bot deletes an event in the transaction that
saves its immediate recipients’ tasks. An unstarted filter and all scheduled
intervals, including 15 minutes, use their existing durable filter cursors
and scan the saved listings. Sends recheck access and current filters.

The bot processes recipient tasks independently. It can retry an explicit
failure up to eight times within the personal lookback. An interrupted or
uncertain Telegram request has a 15-minute lease and can be tried again under
the existing policy. Telegram can accept a request before its reply is lost,
so that policy can deliver a duplicate. The sent journal suppresses repeats
after a confirmed send. Expired tasks and attempts are cleaned by the bot.

## Release

Push the exact candidate to a GitHub branch first. On the VPS, download
`scripts/release.sh` from that SHA, then run:

```bash
bash release.sh stage "$SHA" refs/heads/codex/worker-split
# Publish the tested commit to main and create the corresponding tag on GitHub.
bash release.sh deploy "$SHA" v0.4.1
bash release.sh verify "$SHA"
```

The stage runs Linux tests from the candidate checkout and tests the migration
twice against a protected SQLite backup. The deploy command checks GitHub
`main` and the peeled tag before switching. It records the previous commit,
enabled units, and configuration, creates an online backup, then stops the
writers and takes the definitive backup. It runs `init-db` once with all
writers stopped. Each role logs its imported code path, full SHA and PID.
Verification matches that log to its current systemd invocation and checks
the bot's polling startup. `/cc apply` directs the operator to this procedure.

```bash
systemctl status rentradar.service 'rentradar-collector@*.service'
journalctl -u rentradar-collector@avito.service -n 100 --no-pager
journalctl -u rentradar-avito-detail.service -n 100 --no-pager
systemctl restart rentradar-collector@avito.service
bash /opt/rentradar-releases/current/scripts/release.sh rollback "$BACKUP_PATH"
```

For rollback use the exact `BACKUP_PATH` printed at release. A failed
switch attempts rollback automatically. Rollback stops the split services
before restoring the previous units and version. It retains the current
database, including deliveries and payments made since the upgrade. The
previous single-process version can read the additive schema. If production
has rolled back, publish a normal revert commit on GitHub before the next
switch so the source of record and server agree.

`/health` shows per-source state, heartbeat age, usable search, complete pass,
new and changed counts, the detail backlog and the delivery queue's age. A
source incident clears after a usable search result. The cadence pilot runs
Avito search every three minutes and still attempts one detail per six-minute
cycle. A separate six-minute base cooldown applies after an Avito refusal.
The VPS pilot keeps its one-page search depth. Compare freshness and source
rejection rates before adjusting the cadence, depth or detail budget.
`/collectnow` requests work from live owners and reports when a pass is already
running. A hung async pass hits its configured deadline. A blocked event loop
loses its systemd watchdog heartbeat. Both paths restart that collector only.

Search commits create or refresh the detail task for any changed Avito card
that needs enrichment. The task holds the revision of the fields it can
update. An expired lease is eligible after a restart. Result and delivery
event are committed together, with the lease and revision checked at commit.
The parser runs outside the search process with its own deadline and Linux
address-space limit. HTTP `403` and `429` pause Avito on that route, keep the
deadline across restarts and honour `Retry-After`.

For rollout evidence, run the read-only `scripts/observe_release.py` with
`--since-epoch` set to the deployed bot's start time. It emits reports after
two and 24 hours. Pass `--seconds 300` for a five-minute smoke report.

## Local development

Run `python -m rentradar init-db` once. Start `python -m rentradar serve` and
`python -m rentradar worker --source avito` (likewise `cian` and `yandex`) in
separate terminals with the same settings and database. Run
`python -m rentradar enrich-avito` for the details role. The one-shot
`collect` and `run` commands are available while the workers are stopped.
