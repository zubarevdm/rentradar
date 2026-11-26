#!/usr/bin/env bash
# Run as root on the VPS. Usage and recovery commands are in docs/worker-split.md.
set -Eeuo pipefail
umask 077
export GIT_TERMINAL_PROMPT=0
remote=https://github.com/zubarevdm/rentradar.git
live=/opt/rentradar
releases=/opt/rentradar-releases
backups=/var/backups/rentradar
python="$live/.venv/bin/python"
legacy_units=(rentradar.service rentradar-collector@avito.service rentradar-collector@cian.service
              rentradar-collector@yandex.service)
detail_unit=rentradar-avito-detail.service
units=("${legacy_units[@]}" "$detail_unit")
templates=(rentradar.service rentradar-collector@.service "$detail_unit")
system=/etc/systemd/system

die() { echo "$*" >&2; exit 1; }
full_sha() { [[ ${1:-} =~ ^[a-f0-9]{40}$ ]]; }
[[ $EUID == 0 ]] || die "This script requires root"
install -d -m 755 "$releases"
exec 9>/run/lock/rentradar-release.lock
flock -n 9 || die "Another release is running"
cd "$live"
export RENTRADAR_DB_PATH="$live/data/rentradar.db"

remote_sha() { git ls-remote "$remote" "$1" | cut -f1; }

validate_release() {
    local head tagged
    [[ "$2" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Invalid release tag"
    head=$(remote_sha refs/heads/main)
    tagged=$(remote_sha "refs/tags/$2^{}")
    if [[ -z $tagged ]]; then tagged=$(remote_sha "refs/tags/$2"); fi
    [[ $head == "$1" && $tagged == "$1" ]] || die "GitHub main and tag must both point to $1"
}

get_candidate() {
    local sha=$1 ref=$2 path="$releases/$1"
    [[ $(remote_sha "$ref") == "$sha" ]] || die "GitHub ref changed: $ref"
    if [[ ! -d $path/.git ]]; then
        git init -q "$path"
        git -C "$path" remote add origin "$remote"
    fi
    git -C "$path" fetch -q --no-tags origin "$ref"
    [[ $(git -C "$path" rev-parse FETCH_HEAD) == "$sha" ]] || die "Fetched a different SHA"
    git -C "$path" switch -q --detach "$sha"
    [[ -z $(git -C "$path" status --porcelain --untracked-files=no) ]] ||
        die "Candidate has local modifications"
}

verify() {
    local sha=$1 path="$releases/$1" u pid invocation stamp role source line
    [[ $(readlink -f "$releases/current") == "$path" ]] || return 1
    local -a expected=("${legacy_units[@]}")
    if [[ -f $path/scripts/systemd/$detail_unit ]]; then expected+=("$detail_unit"); fi
    for u in "${expected[@]}"; do
        systemctl is-active --quiet "$u" && systemctl is-enabled --quiet "$u" || return 1
        pid=$(systemctl show "$u" --property=MainPID --value)
        [[ $pid =~ ^[1-9][0-9]*$ ]] || return 1
        invocation=$(systemctl show "$u" --property=InvocationID --value)
        stamp=$(systemctl show "$u" --property=ExecMainStartTimestamp --value)
        role=bot
        source=-
        if [[ $u == *@* ]]; then
            role=collector
            source=${u#*@}
            source=${source%.service}
        elif [[ $u == "$detail_unit" ]]; then
            role=enrichment
            source=avito
        fi
        line="runtime_start role=$role source=$source pid=$pid sha=$sha path=$path/src/rentradar/runtime.py"
        journalctl "_SYSTEMD_INVOCATION_ID=$invocation" "_PID=$pid" --no-pager -o cat |
            grep -F -- "$line" >/dev/null || return 1
        if [[ $role == bot ]]; then
            journalctl "_SYSTEMD_INVOCATION_ID=$invocation" "_PID=$pid" --no-pager -o cat |
                grep -F 'Start polling' >/dev/null || return 1
        fi
        printf '%s %s %s %s\n' "$u" "$sha" "$pid" "$stamp"
    done
}

restore() {
    local backup=$1 old target name u
    read -r old < "$backup/commit"
    full_sha "$old" || return 1
    if [[ -s $backup/current ]]; then
        read -r target < "$backup/current"
        [[ $target == "$releases/$old" && $(git -C "$target" rev-parse HEAD) == "$old" ]] ||
            return 1
    else
        [[ $(git -C "$live" rev-parse HEAD) == "$old" ]] || return 1
    fi
    for u in "${units[@]}"; do
        if [[ $(systemctl show "$u" -p LoadState --value) != not-found ]]; then
            systemctl stop "$u" || return 1
        fi
        if systemctl is-enabled --quiet "$u"; then systemctl disable "$u" || return 1; fi
    done
    for name in "${templates[@]}"; do
        if [[ -f $backup/$name ]]; then
            cp -p -- "$backup/$name" "$system/$name" || return 1
        else
            rm -f -- "$system/$name" || return 1
        fi
    done
    if [[ -n ${target:-} ]]; then
        ln -sfn -- "$target" "$releases/current" || return 1
    else
        rm -f -- "$releases/current" || return 1
    fi
    systemctl daemon-reload || return 1
    local -a enabled
    mapfile -t enabled < "$backup/enabled"
    systemctl enable "${enabled[@]}" && systemctl start "${enabled[@]}" || return 1
    systemctl is-active --quiet rentradar.service || return 1
    if [[ -n ${target:-} ]]; then
        local attempt
        for attempt in {1..45}; do
            if verify "$old"; then break; fi
            sleep 1
        done
        verify "$old" >/dev/null || return 1
    else
        local pid
        pid=$(systemctl show rentradar.service -p MainPID --value)
        [[ $pid != 0 && $(readlink -f "/proc/$pid/cwd") == "$live" ]] || return 1
    fi
    echo "ROLLED_BACK $old (live SQLite retained)"
}

case "${1:-}" in
    stage)
        sha=${2:-}; ref=${3:-}
        full_sha "$sha" || die "Use a full candidate SHA"
        [[ $ref == refs/heads/* ]] || die "Use a GitHub branch ref"
        get_candidate "$sha" "$ref"
        path="$releases/$sha"
        tmp=$(mktemp -d "$backups/stage-XXXXXXXX")
        trap 'rm -f -- "$tmp/migration.db" "$tmp/migration.db-wal" "$tmp/migration.db-shm"' EXIT
        export PYTHONPATH="$path/src" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
        "$python" -c 'import pathlib,rentradar,sys; assert pathlib.Path(rentradar.__file__).is_relative_to(sys.argv[1]); print(rentradar.__file__)' "$path"
        for script in "$path"/scripts/{release,run-role,cc_apply}.sh; do bash -n "$script"; done
        systemd-analyze verify "$path"/scripts/systemd/*.service
        (
            cd "$path"
            unset RENTRADAR_DB_PATH
            "$python" -m pytest -p pytest_asyncio.plugin -q --basetemp "$tmp/pytest"
            RENTRADAR_TEST_SYSTEMD=1 "$python" -m pytest -p pytest_asyncio.plugin -q \
                --basetemp "$tmp/systemd" tests/test_worker_processes.py
        ) | tee "$tmp/tests.log"
        "$python" "$path/scripts/check_database.py" "$tmp/migration.db" --test-migration |
            tee "$tmp/migration.log"
        previous=$live
        if [[ -L $releases/current ]]; then previous=$(readlink -f "$releases/current"); fi
        RENTRADAR_DB_PATH="$tmp/migration.db" PYTHONPATH="$previous/src" \
            "$python" -m rentradar init-db
        echo "PREVIOUS_RELEASE_SCHEMA_OK $(git -C "$previous" rev-parse HEAD)" |
            tee -a "$tmp/migration.log"
        printf '%s\n' "$tmp" > "$releases/$sha.verified"
        echo "STAGED $sha EVIDENCE $tmp"
        ;;
    deploy)
        sha=${2:-}; tag=${3:-}
        full_sha "$sha" || die "Use a full release SHA"
        validate_release "$sha" "$tag"
        path="$releases/$sha"
        [[ -s $releases/$sha.verified ]] || die "Run stage first"
        get_candidate "$sha" refs/heads/main
        if [[ $(readlink -f "$releases/current") == "$path" ]] && verify "$sha"; then exit 0; fi
        systemctl is-active --quiet rentradar.service
        [[ -z $(systemctl show rentradar.service -p DropInPaths --value) ]] ||
            die "The current service has overrides that need to be integrated"
        [[ -z $(systemctl show rentradar.service -p User --value) ]] ||
            die "Adapt the unit and file ownership to the current service user"
        backup=$(mktemp -d "$backups/worker-split-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXXXX")
        if [[ -L $releases/current ]]; then
            readlink -f "$releases/current" > "$backup/current"
            git -C "$(cat "$backup/current")" rev-parse HEAD > "$backup/commit"
        else
            git -C "$live" rev-parse HEAD > "$backup/commit"
        fi
        branch=$(remote_sha "refs/tags/v0.2.0^{}")
        if [[ ! -s $backup/current && $(cat "$backup/commit") != "$branch" ]]; then
            die "The single-process rollback target must match the published v0.2.0"
        fi
        for name in "${templates[@]}"; do
            if [[ -f $system/$name ]]; then cp -p -- "$system/$name" "$backup/$name"; fi
        done
        cp -p -- "$live/.env" "$backup/server.env"
        for name in "${units[@]}"; do
            if systemctl is-enabled --quiet "$name" 2>/dev/null; then echo "$name"; fi
        done > "$backup/enabled"
        grep -Fxq rentradar.service "$backup/enabled" || die "Current bot is not enabled"
        export PYTHONPATH="$path/src"
        "$python" "$path/scripts/check_database.py" "$backup/online.sqlite"
        validate_release "$sha" "$tag"
        echo "BACKUP_PATH $backup"
        on_failure() {
            trap - ERR
            echo "RELEASE_FAILED $sha BACKUP_PATH $backup" >&2
            restore "$backup" || echo "ROLLBACK_FAILED $backup" >&2
            exit 1
        }
        trap on_failure ERR
        systemctl stop "${units[0]}"
        if [[ -s $backup/current ]]; then
            for name in "${units[@]:1}"; do
                if [[ $(systemctl show "$name" -p LoadState --value) != not-found ]]; then
                    systemctl stop "$name"
                fi
            done
        fi
        "$python" "$path/scripts/check_database.py" "$backup/database.sqlite"
        "$python" -m rentradar init-db
        ln -sfn -- "$path" "$releases/current"
        for name in "${templates[@]}"; do
            install -m 644 "$path/scripts/systemd/$name" "$system/$name"
        done
        systemctl daemon-reload
        systemctl enable "${units[@]}"
        systemctl start "${units[@]}"
        for attempt in {1..45}; do
            if verify "$sha"; then break; fi
            sleep 1
        done
        verify "$sha" >/dev/null
        trap - ERR
        echo "DEPLOYED $sha $tag BACKUP_PATH $backup"
        ;;
    verify)
        full_sha "${2:-}" || die "Use a full release SHA"
        verify "$2"
        ;;
    rollback)
        backup=$(realpath -e -- "${2:-}")
        [[ -d $backup && $backup == "$backups"/worker-split-* && -s $backup/commit ]] ||
            die "Use a recorded release backup path"
        restore "$backup"
        ;;
    *) die "Usage: release.sh stage SHA refs/heads/BRANCH | deploy SHA TAG | verify SHA | rollback BACKUP" ;;
esac
