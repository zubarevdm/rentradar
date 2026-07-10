#!/usr/bin/env bash
# Запуск задачи Claude Code в ИЗОЛИРОВАННОМ git-worktree (не трогает живой сервис).
# Печатает РОВНО одну строку JSON в stdout (её парсит бот). Всё прочее — в stderr/файлы.
#
# Поток: свежая ветка cc/<ts> от origin/main → claude правит → git diff → pytest →
# коммит+пуш ветки. Применение на прод — отдельно, cc_apply.sh (по /cc apply).
set -uo pipefail

TASK="${1:-}"
[ -z "$TASK" ] && { echo '{"status":"error","msg":"empty task"}'; exit 0; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
WT="${REPO}-cc"

# Single-flight: одна задача /cc за раз.
exec 9>"/tmp/rentradar-cc.lock"
if ! flock -n 9; then echo '{"status":"busy"}'; exit 0; fi

TOKEN="$(grep '^RENTRADAR_CLAUDE_TOKEN=' "$REPO/.env" 2>/dev/null | cut -d= -f2- | tr -d '<>"'"'"'\r ')"
[ -z "$TOKEN" ] && { echo '{"status":"error","msg":"no token in .env"}'; exit 0; }
export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"

[ -d "$WT/.git" ] || [ -f "$WT/.git" ] || { echo '{"status":"error","msg":"worktree not set up"}'; exit 0; }

TS="$(date +%Y%m%d-%H%M%S)"
BRANCH="cc/$TS"
cd "$WT" || { echo '{"status":"error","msg":"cd worktree failed"}'; exit 0; }
git fetch -q origin 2>/dev/null
git checkout -q -B "$BRANCH" origin/main 2>/dev/null
git clean -qfd 2>/dev/null

# Claude правит код. acceptEdits + узкий allowedTools: правки и тесты без запросов,
# git/сеть/разрушительное — недоступны (в headless просто отклоняются, не висят).
claude -p "$TASK" \
  --permission-mode acceptEdits \
  --allowedTools "Read,Edit,Write,Bash(pytest*),Bash(python -m pytest*),Bash(ls*)" \
  --output-format json >/tmp/rentradar-cc-claude.json 2>/tmp/rentradar-cc-claude.err
SUMMARY="$(jq -r '.result // "(нет ответа Claude)"' /tmp/rentradar-cc-claude.json 2>/dev/null)"
COST="$(jq -r '.total_cost_usd // 0' /tmp/rentradar-cc-claude.json 2>/dev/null)"
[ -z "$COST" ] && COST=0

git add -A 2>/dev/null
CHANGED="$(git diff --cached --name-only 2>/dev/null | grep -c . || true)"
if [ "${CHANGED:-0}" = "0" ]; then
  jq -nc --arg s "$SUMMARY" --argjson c "$COST" '{status:"no_changes",summary:$s,cost:$c}'
  exit 0
fi
DIFFSTAT="$(git diff --cached --stat 2>/dev/null | tail -30)"

# Тесты в собственном venv worktree (изолированно от боевого).
"$WT/.venv/bin/python" -m pytest -q >/tmp/rentradar-cc-pytest.log 2>&1
PYTEST_RC=$?
PYTEST_TAIL="$(tail -4 /tmp/rentradar-cc-pytest.log)"

git commit -q -m "cc: $TASK" 2>/dev/null
git push -qf origin "$BRANCH" 2>/dev/null
echo "$BRANCH" > "$REPO/.cc_pending"

jq -nc --arg br "$BRANCH" --arg s "$SUMMARY" --arg ds "$DIFFSTAT" \
  --argjson ch "$CHANGED" --argjson rc "$PYTEST_RC" --arg pt "$PYTEST_TAIL" --argjson c "$COST" \
  '{status:"done",branch:$br,summary:$s,diffstat:$ds,changed:$ch,pytest_rc:$rc,pytest:$pt,cost:$c}'
