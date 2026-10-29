#!/usr/bin/env bash
# #456 follow-up: run the operator guidance's commands on real runtime homes.
#
# Run from the openminion checkout on the Linux runner (after `make dev-install`):
#   bash transcript_456.sh <out_dir>
#
# Setup, not part of the guidance (lines marked [setup]):
# - a separate venv with openminion==0.1.14 from PyPI, whose task.consolidate_memory
#   stores no task kind;
# - runtime homes with an echo agent, written with 0.1.14's config API;
# - the pre-upgrade consolidation job in each home, created with 0.1.14's own CLI
#   (`openminion tools run task.consolidate_memory`);
# - to let a scheduled run happen without waiting for the interval, the job's
#   cron_jobs.next_due_at is set to now (`due_now`).
# Lines marked [evidence] read the task database directly (the CLI does not show
# the task kind). Every `$ openminion ...` line is the checkout's own CLI
# (.venv/bin/openminion) unless it says openminion(0.1.14).
set -u -o pipefail
OUT="$(mkdir -p "$1" && cd "$1" && pwd)"
REPO="$(pwd)"
PY="$REPO/.venv/bin/python"
OM="$REPO/.venv/bin/openminion"
W="$(mktemp -d)"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_AGENT_ID
export PYTHONUTF8=1
T="$OUT/transcript.txt"
: > "$T"

note() { printf '\n# %s\n' "$*" | tee -a "$T"; }

show_args() {
  local out="" a
  for a in "$@"; do
    if [[ "$a" =~ [^A-Za-z0-9._/:=-] ]]; then out+="'$a' "; else out+="$a "; fi
  done
  printf '%s' "${out% }"
}

# run HOME BIN ARGS...: print the command with placeholders, run it, keep its output.
# The INFO log lines on stderr are dropped from the transcript (kept in stderr.log).
run() {
  local home="$1" bin="$2"; shift 2
  local shown="openminion"
  [[ "$bin" == "$W/v14/bin/openminion" ]] && shown="openminion(0.1.14)"
  printf '\n$ %s%s --home-root <home> --data-root <home>/.openminion --config <home>/agents.json --no-update-check %s\n' \
    "${ENV_SHOWN:-}" "$shown" "$(show_args "$@")" | tee -a "$T"
  (cd "$home" && timeout 300 "$bin" --home-root "$home" --data-root "$home/.openminion" --config "$home/agents.json" \
      --no-update-check "$@") > "$W/last.out" 2> "$W/last.err"
  local rc=$?
  sed -e "s#$W#<work>#g" -e "s#$REPO#<repo>#g" "$W/last.out" | tee -a "$T"
  { echo "### $shown $*"; cat "$W/last.err"; } | sed -e "s#$W#<work>#g" -e "s#$REPO#<repo>#g" >> "$OUT/stderr.log"
  if grep -v -E '^[0-9-]+ [0-9:,]+ INFO ' "$W/last.err" | grep -q .; then
    echo "[stderr, without INFO lines]" | tee -a "$T"
    grep -v -E '^[0-9-]+ [0-9:,]+ INFO ' "$W/last.err" | sed -e "s#$W#<work>#g" -e "s#$REPO#<repo>#g" | tail -20 | tee -a "$T"
  fi
  echo "[exit $rc]" | tee -a "$T"
  return 0
}

created_id() { "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(((d.get("tool") or {}).get("data") or {}).get("task_id") or d.get("task_id"))' "$W/last.out"; }

# Consolidation tasks in the last `tasks list --json`: id, lifecycle_state, enabled, last run state.
summary() {
  "$PY" - "$W/last.out" <<'EOF' | tee -a "$T"
import json, sys
data = json.load(open(sys.argv[1]))
rows = [t for t in data.get("tasks", []) if str(t.get("instruction", "")).startswith("Review the provided memory candidates")]
print("[summary] consolidation tasks:")
for t in rows:
    last = t.get("last_run") or {}
    print(f"[summary]   {t['id']}  agent_id={t.get('agent_id')}  lifecycle_state={t.get('lifecycle_state')}  "
          f"enabled={t.get('enabled')}  last_run={last.get('state')}  valid_actions={t.get('valid_actions')}")
print("[summary] enabled consolidation tasks:", [t["id"] for t in rows if t.get("enabled")])
EOF
}

make_home() {
  local home="$1" agent="$2"
  mkdir -p "$home"
  "$W/v14/bin/python" -I - "$home" "$agent" <<'EOF' | tee -a "$T"
import sys
from pathlib import Path
from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config
root, agent = Path(sys.argv[1]), sys.argv[2]
config = OpenMinionConfig()
config.agents = {agent: AgentProfileConfig(name=agent, provider="echo", default_channel="console")}
config.default_agent = agent
config.runtime.daemon_auto_start = False
save_config(config, str(root / "agents.json"))
print(f"[setup] {root.name}/agents.json: echo agent {agent!r} (default agent), daemon_auto_start false")
EOF
}

due_now() {
  local home="$1" job="$2"
  "$PY" -I - "$home" "$job" <<'EOF' | tee -a "$T"
import sqlite3, sys
from pathlib import Path
from openminion.modules.task.scheduling.schedule import to_iso_utc, utc_now
home, job = Path(sys.argv[1]), sys.argv[2]
for db in sorted(home.rglob("*.db")):
    con = sqlite3.connect(db)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cron_jobs'").fetchone():
            continue
        before = con.execute("SELECT next_due_at FROM cron_jobs WHERE job_id = ?", (job,)).fetchone()
        if before is None:
            continue
        now = to_iso_utc(utc_now())
        con.execute("UPDATE cron_jobs SET next_due_at = ? WHERE job_id = ?", (now, job))
        con.commit()
        print(f"[setup] {db.relative_to(home)}: cron_jobs.next_due_at of {job}: {before[0]} -> {now}")
    finally:
        con.close()
EOF
}

last_run_id() {
  (cd "$1" && timeout 120 "$OM" --home-root "$1" --data-root "$1/.openminion" --config "$1/agents.json" \
     --no-update-check tasks show "$2" --json) 2>/dev/null \
    | "$PY" -c 'import json,sys; t=json.load(sys.stdin).get("task") or {}; r=t.get("last_run") or {}; print(r.get("run_id") or "", r.get("state") or "")'
}

# Wait (polling `tasks show`) until the task has a terminal run other than PREV.
wait_new_run() {
  local home="$1" task="$2" prev="$3" id state
  for _ in $(seq 1 120); do
    read -r id state <<< "$(last_run_id "$home" "$task")"
    if [[ -n "$id" && "$id" != "$prev" && "$state" =~ ^(finished|failed|timed_out|cancelled)$ ]]; then
      echo "[wait] new run $id: $state" | tee -a "$T"
      return 0
    fi
    sleep 1
  done
  echo "[wait] timed out: last run $id $state" | tee -a "$T"
}

task_kind() {
  "$PY" -I - "$1" "$2" <<'EOF' | tee -a "$T"
import json, sqlite3, sys
from pathlib import Path
home, task = Path(sys.argv[1]), sys.argv[2]
db = home / ".openminion" / "task" / "task.db"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
try:
    row = con.execute("SELECT state, metadata FROM scheduled_tasks WHERE task_id = ?", (task,)).fetchone()
finally:
    con.close()
print(f"[evidence] .openminion/task/task.db scheduled_tasks {task}: state={row[0]}, metadata.kind={json.loads(row[1]).get('kind')!r}")
EOF
}

note "setup: openminion==0.1.14 from PyPI in a separate venv"
python -m venv "$W/v14" && "$W/v14/bin/pip" install -q "openminion==0.1.14" > "$OUT/pip-0.1.14.log" 2>&1
"$W/v14/bin/pip" freeze > "$OUT/pip-freeze-0.1.14.txt"
"$W/v14/bin/python" -I -c "import openminion, sys; print('[setup] 0.1.14 venv: openminion', openminion.__version__, 'CPython', sys.version.split()[0])" | tee -a "$T"
"$PY" -I -c "import openminion, sqlite3, sys; print('[setup] checkout: openminion', openminion.__version__, 'CPython', sys.version.split()[0], 'SQLite', sqlite3.sqlite_version)" | tee -a "$T"
echo "[setup] checkout head: $(git log --oneline -1)" | tee -a "$T"

########################################################################## home A
A="$W/homeA"
note "A: a pre-upgrade job, stopped (pause) before it runs on this package; replacement created while the daemon runs"
make_home "$A" openminion
run "$A" "$W/v14/bin/openminion" tools run task.consolidate_memory --json '{"interval_hours": 12, "batch_limit": 8}'
OLD_A="$(created_id)"
echo "[setup] pre-upgrade task in A: $OLD_A" | tee -a "$T"
task_kind "$A" "$OLD_A"

note "A. inspect"
run "$A" "$OM" tasks list --json
summary
run "$A" "$OM" tasks show "$OLD_A" --json
run "$A" "$OM" tools run task.list --json '{"limit": 50}'

note "A. stop the old job (pause), then create the replacement with the daemon running"
run "$A" "$OM" tasks pause "$OLD_A" --json
run "$A" "$OM" daemon start
run "$A" "$OM" tools run task.consolidate_memory --json '{"interval_hours": 12, "batch_limit": 8}'
NEW_A="$(created_id)"
echo "[replacement in A: $NEW_A]" | tee -a "$T"
task_kind "$A" "$NEW_A"

note "A. check: only the replacement is enabled"
run "$A" "$OM" tasks list --json
summary

note "A. the replacement's scheduled run under the daemon is a consolidation run"
due_now "$A" "$NEW_A"
wait_new_run "$A" "$NEW_A" ""
run "$A" "$OM" tasks show "$NEW_A" --json
run "$A" "$OM" daemon stop
run "$A" "$OM" tasks list --json
summary

########################################################################## home B
B="$W/homeB"
note "B: a pre-upgrade job that the daemon runs on this package"
make_home "$B" openminion
run "$B" "$W/v14/bin/openminion" tools run task.consolidate_memory --json '{"interval_hours": 6, "batch_limit": 5}'
OLD_B="$(created_id)"
echo "[setup] pre-upgrade task in B: $OLD_B" | tee -a "$T"
due_now "$B" "$OLD_B"
run "$B" "$OM" daemon start
wait_new_run "$B" "$OLD_B" ""
read -r FIRST_B _ <<< "$(last_run_id "$B" "$OLD_B")"

note "B. inspect the paused job"
run "$B" "$OM" tasks list --json
summary
run "$B" "$OM" tasks show "$OLD_B" --json
run "$B" "$OM" tools run task.list --json '{"limit": 50}'

note "B. resuming it does not help: its next run fails and pauses it again"
run "$B" "$OM" tasks resume "$OLD_B" --json
due_now "$B" "$OLD_B"
wait_new_run "$B" "$OLD_B" "$FIRST_B"
run "$B" "$OM" daemon stop
run "$B" "$OM" tasks show "$OLD_B" --json

note "B. stop the old job (cancel, on the paused job), then create the replacement"
run "$B" "$OM" tasks cancel "$OLD_B" --json
run "$B" "$OM" tools run task.consolidate_memory --json '{"interval_hours": 6, "batch_limit": 5}'
NEW_B="$(created_id)"
echo "[replacement in B: $NEW_B]" | tee -a "$T"
task_kind "$B" "$NEW_B"

note "B. check: only the replacement is enabled"
run "$B" "$OM" tasks list --json
summary

note "B. raw cron API: a new job with the reserved payload is rejected"
(cd "$B" && "$OM" --home-root "$B" --data-root "$B/.openminion" --config "$B/agents.json" --no-update-check \
   api run --host 127.0.0.1 --port 18766) > "$W/api.out" 2>&1 &
API_PID=$!
for _ in $(seq 1 90); do grep -q "listening" "$W/api.out" 2>/dev/null && break; sleep 1; done
echo '$ openminion --home-root <home> --data-root <home>/.openminion --config <home>/agents.json --no-update-check api run --host 127.0.0.1 --port 18766 &' | tee -a "$T"
grep -v -E '^[0-9-]+ [0-9:,]+ INFO ' "$W/api.out" | sed -e "s#$W#<work>#g" | tee -a "$T"
BODY='{"name": "raw consolidation", "agent_id": "openminion", "schedule": {"kind": "every", "every_ms": 43200000}, "payload": {"kind": "agentTurn", "message": "review memory candidates", "_openminion_memory_consolidation": {"target_scope": "agent:openminion"}}}'
printf "\n\$ curl -sS -X POST http://127.0.0.1:18766/v1/cron/jobs -H 'Content-Type: application/json' -d '%s'\n" "$BODY" | tee -a "$T"
curl -sS -X POST http://127.0.0.1:18766/v1/cron/jobs -H "Content-Type: application/json" -d "$BODY" -w '\n[http %{http_code}]\n' | tee -a "$T"
printf '\n$ curl -sS http://127.0.0.1:18766/v1/cron/jobs   (job ids and names only)\n' | tee -a "$T"
curl -sS http://127.0.0.1:18766/v1/cron/jobs | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print({"ok": d.get("ok"), "jobs": [(j["job_id"], j["name"], j["enabled"]) for j in d.get("jobs", [])]})' | tee -a "$T"
kill "$API_PID" 2>/dev/null; wait "$API_PID" 2>/dev/null
run "$B" "$OM" tasks list --json
summary

########################################################################## home C
C="$W/homeC"
note "C: the default agent is 'main'; a pre-upgrade job of 'main', replaced for 'main'"
make_home "$C" main
ENV_SHOWN="OPENMINION_AGENT_ID=main " ; export OPENMINION_AGENT_ID=main
run "$C" "$W/v14/bin/openminion" tools run task.consolidate_memory --json '{"interval_hours": 24, "batch_limit": 10}'
OLD_C="$(created_id)"
unset OPENMINION_AGENT_ID; ENV_SHOWN=""
echo "[setup] pre-upgrade task in C: $OLD_C" | tee -a "$T"
task_kind "$C" "$OLD_C"
run "$C" "$OM" tasks list --json
summary
run "$C" "$OM" tasks show "$OLD_C" --json
ENV_SHOWN="OPENMINION_AGENT_ID=main " ; export OPENMINION_AGENT_ID=main
run "$C" "$OM" tools run task.list --runtime-source inproc --json '{"limit": 50}'
unset OPENMINION_AGENT_ID; ENV_SHOWN=""
run "$C" "$OM" tools run task.list --json '{"limit": 50}'
run "$C" "$OM" tasks cancel "$OLD_C" --json

note "C. without OPENMINION_AGENT_ID the tool creates the job for 'openminion' (observation)"
run "$C" "$OM" tools run task.consolidate_memory --json '{"interval_hours": 24, "batch_limit": 10}'
STRAY_C="$(created_id)"
run "$C" "$OM" tasks list --agent-id openminion --json
summary
run "$C" "$OM" tasks cancel "$STRAY_C" --agent-id openminion --json

note "C. with the daemon running: OPENMINION_AGENT_ID without and with --runtime-source inproc"
run "$C" "$OM" daemon start
ENV_SHOWN="OPENMINION_AGENT_ID=main " ; export OPENMINION_AGENT_ID=main
run "$C" "$OM" tools run task.consolidate_memory --json '{"interval_hours": 24, "batch_limit": 10}'
VIA_DAEMON_C="$(created_id)"
run "$C" "$OM" tools run task.consolidate_memory --runtime-source inproc --json '{"interval_hours": 24, "batch_limit": 10}'
NEW_C="$(created_id)"
unset OPENMINION_AGENT_ID; ENV_SHOWN=""
echo "[created via the daemon: $VIA_DAEMON_C; replacement in C (inproc): $NEW_C]" | tee -a "$T"
task_kind "$C" "$NEW_C"
run "$C" "$OM" tasks list --json
summary
run "$C" "$OM" tasks list --agent-id openminion --json
summary

note "C. the replacement's scheduled run under the daemon is a consolidation run"
due_now "$C" "$NEW_C"
wait_new_run "$C" "$NEW_C" ""
run "$C" "$OM" daemon stop
run "$C" "$OM" tasks list --json
summary

########################################################################## home D
D="$W/homeD"
note "D (what the guidance warns about): the replacement created while the old job is still enabled"
make_home "$D" openminion
run "$D" "$W/v14/bin/openminion" tools run task.consolidate_memory --json '{"interval_hours": 12, "batch_limit": 8}'
OLD_D="$(created_id)"
echo "[setup] pre-upgrade task in D: $OLD_D" | tee -a "$T"
run "$D" "$OM" tools run task.consolidate_memory --json '{"interval_hours": 12, "batch_limit": 8}'
NEW_D="$(created_id)"
run "$D" "$OM" tasks list --json
summary
due_now "$D" "$OLD_D"
run "$D" "$OM" daemon start
wait_new_run "$D" "$OLD_D" ""
run "$D" "$OM" daemon stop
run "$D" "$OM" tasks list --json
summary

note "done"
