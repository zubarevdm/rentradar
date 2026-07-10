#!/usr/bin/env bash
# Влить ожидающую cc-ветку в main (боевой репозиторий) и запушить. НЕ перезапускает
# сервис — рестарт делает бот ПОСЛЕ отправки подтверждения (иначе убьёт сам себя до
# ответа). Печатает одну строку JSON. Вызывается по /cc apply.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
BRANCH="$(cat "$REPO/.cc_pending" 2>/dev/null | tr -d ' \r\n')"
[ -z "$BRANCH" ] && { echo '{"status":"nothing"}'; exit 0; }

cd "$REPO" || { echo '{"status":"error","msg":"cd repo failed"}'; exit 0; }
git fetch -q origin 2>/dev/null
git checkout -q main 2>/dev/null
if ! git merge -q --no-edit "origin/$BRANCH" >/tmp/rentradar-cc-merge.log 2>&1; then
  git merge --abort 2>/dev/null
  jq -nc --arg b "$BRANCH" '{status:"conflict",branch:$b}'
  exit 0
fi
git push -q origin main 2>/dev/null
git push -q origin --delete "$BRANCH" 2>/dev/null   # прибрать влитую ветку с GitHub
rm -f "$REPO/.cc_pending"
jq -nc --arg b "$BRANCH" '{status:"applied",branch:$b}'
