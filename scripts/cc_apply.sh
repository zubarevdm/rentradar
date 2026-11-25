#!/usr/bin/env bash
# Влить ожидающую cc-ветку в main (боевой репозиторий) и запушить. НЕ перезапускает
# сервис — рестарт делает бот ПОСЛЕ отправки подтверждения (иначе убьёт сам себя до
# ответа). Печатает одну строку JSON. Вызывается по /cc apply.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
fail() { jq -cn --arg msg "$1" '{status:"error",msg:$msg}'; exit 1; }
exec 9>"/tmp/rentradar-cc.lock"
flock -n 9 || fail "another release is running"
[ -s "$REPO/.cc_pending" ] || { echo '{"status":"nothing"}'; exit 0; }
read -r BRANCH tested < "$REPO/.cc_pending" || fail "invalid release record"
[[ "$BRANCH" == cc/* && "$tested" =~ ^[[:xdigit:]]{40}$ ]] || fail "candidate requires a tested commit"
cd "$REPO" || fail "cannot open deployment"
git diff --quiet && git diff --cached --quiet || fail "working tree has changes"
git fetch -q origin || fail "fetch failed"
target=$(git rev-parse --verify "refs/remotes/origin/$BRANCH^{commit}") || fail "unknown branch"
[ "$target" = "$tested" ] || fail "candidate changed after testing"
git merge-base --is-ancestor origin/main "$target" || fail "candidate must be rebased and tested"
git merge-base --is-ancestor HEAD "$target" || fail "deployment has diverged"
git push -q origin "$target:refs/heads/main" || fail "GitHub rejected release"
remote=$(git ls-remote origin refs/heads/main) || fail "cannot verify published release"
[ "${remote%%[[:space:]]*}" = "$target" ] || fail "GitHub SHA changed"
git switch --detach "$target" || fail "cannot deploy verified GitHub SHA"
rm -f "$REPO/.cc_pending"
jq -cn --arg b "$BRANCH" '{status:"applied",branch:$b}'
