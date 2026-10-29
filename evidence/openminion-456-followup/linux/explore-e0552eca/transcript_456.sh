#!/usr/bin/env bash
# #456 follow-up: run the operator guidance's commands on real runtime homes.
#
# Run from the openminion checkout on the Linux runner (after `make dev-install`):
#   bash transcript_456.sh <out_dir>
#
# Setup, not part of the guidance:
# - a separate venv with openminion==0.1.14 from PyPI (the previously published
#   task.consolidate_memory, which stores no task kind);
# - runtime homes A and B with an echo agent, written with 0.1.14's config API;
# - the legacy consolidation job in each home, created with 0.1.14's own CLI
#   (`openminion tools run task.consolidate_memory`);
# - to let a scheduled run happen without waiting 12 hours, the job's
#   next_due_at is set to now in the cron table (`due_now`).
# Everything else is the checkout's own `openminion` CLI (.venv/bin/openminion).
set -u -o pipefail
OUT="$(mkdir -p "$1" && cd "$1" && pwd)"
REPO="$(pwd)"
PY="$REPO/.venv/bin/python"
OM="$REPO/.venv/bin/openminion"
W="$(mktemp -d)"
unset OPENMINION_HOME OPENMINION_DATA_ROOT
export PYTHONUTF8=1
T="$OUT/transcript.txt"
: > "$T"

note() { printf '\n# %s\n' "$*" | tee -a "$T"; }

show_args() {
  local out="" a
  for a in "$@"; do
    if [[ "$a" =~ [^A-Za-z0-9._/:=-] ]]; then out+="'$a' "; else out+="$a "; fi
  done
  printf '%s' "$out"
}

# run HOME BIN ARGS...: print the command with placeholders, run it, keep its output.
run() {
  local home="$1" bin="$2"; shift 2
  local shown="openminion"
  [[ "$bin" == "$W/v14/bin/openminion" ]] && shown="openminion(0.1.14)"
  printf '\n$ %s --home-root <home> --data-root <home>/.openminion --config <home>/agents.json --no-update-check %s\n' \
    "$shown" "$(show_args "$@")" | tee -a "$T"
  (cd "$home" && timeout 300 "$bin" --home-root "$home" --data-root "$home/.openminion" --config "$home/agents.json" \
      --no-update-check "$@") > "$W/last.out" 2> "$W/last.err"
  local rc=$?
  sed -e "s#$W#<work>#g" -e "s#$REPO#<repo>#g" "$W/last.out" | tee -a "$T"
  if [[ -s "$W/last.err" ]]; then
    echo "[stderr]" | tee -a "$T"
    sed -e "s#$W#<work>#g" -e "s#$REPO#<repo>#g" "$W/last.err" | tail -20 | tee -a "$T"
  fi
  echo "[exit $rc]" | tee -a "$T"
  return 0
}

jget() { "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1]));
for k in sys.argv[2].split("."):
    d = d[int(k)] if isinstance(d, list) else d[k]
print(d)' "$W/last.out" "$1"; }

# Consolidation tasks in `tasks list --json`: id, lifecycle_state, enabled, last run state.
summary() {
  "$PY" - "$W/last.out" <<'EOF' | tee -a "$T"
import json, sys
data = json.load(open(sys.argv[1]))
rows = [t for t in data.get("tasks", []) if str(t.get("instruction", "")).startswith("Review the provided memory candidates")]
print("consolidation tasks:")
for t in rows:
    last = t.get("last_run") or {}
    print(f"  {t['id']}  lifecycle_state={t.get('lifecycle_state')}  enabled={t.get('enabled')}  "
          f"last_run={last.get('state')}  valid_actions={t.get('valid_actions')}")
print("enabled:", [t["id"] for t in rows if t.get("enabled")])
EOF
}

make_home() {
  local home="$1"
  mkdir -p "$home"
  "$W/v14/bin/python" -I - "$home" <<'EOF'
import sys
from pathlib import Path
from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config
root = Path(sys.argv[1])
config = OpenMinionConfig()
config.agents = {"openminion": AgentProfileConfig(name="openminion", provider="echo", default_channel="console")}
config.default_agent = "openminion"
config.runtime.daemon_auto_start = False
save_config(config, str(root / "agents.json"))
print("config:", root.name + "/agents.json", "(echo agent 'openminion', daemon_auto_start false)")
EOF
}

# Setup only: let the job's next scheduled run be due now (instead of in 12 hours).
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

# Wait until the task's last run is in a terminal state (polls `tasks show`).
wait_last_run() {
  local home="$1" task="$2" want="$3"
  for _ in $(seq 1 120); do
    (cd "$home" && timeout 120 "$OM" --home-root "$home" --data-root "$home/.openminion" --config "$home/agents.json" \
       --no-update-check tasks show "$task" --json) > "$W/poll.out" 2>/dev/null
    state="$("$PY" -c 'import json,sys; t=json.load(open(sys.argv[1])).get("task") or {}; print((t.get("last_run") or {}).get("state"))' "$W/poll.out" 2>/dev/null)"
    if [[ "$state" == "$want" || "$state" == "failed" || "$state" == "finished" ]]; then
      echo "[wait] last_run.state=$state" | tee -a "$T"
      return 0
    fi
    sleep 1
  done
  echo "[wait] timed out, last_run.state=$state" | tee -a "$T"
}

# Read the persisted task kind (evidence only: the CLI does not show it).
task_kind() {
  "$PY" -I - "$1" "$2" <<'EOF' | tee -a "$T"
import json, sqlite3, sys
from pathlib import Path
home, task = Path(sys.argv[1]), sys.argv[2]
for db in sorted(home.rglob("*.db")):
    con = sqlite3.connect(db)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in sorted(tables):
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            if "task_id" in cols and "metadata_json" in cols:
                row = con.execute(f'SELECT metadata_json FROM "{table}" WHERE task_id = ?', (task,)).fetchone()
                if row:
                    kind = json.loads(row[0] or "{}").get("kind")
                    print(f"[evidence] {db.relative_to(home)} {table}: task {task} metadata.kind = {kind!r}")
    finally:
        con.close()
EOF
}

note "setup: openminion==0.1.14 from PyPI in a separate venv (the previously published task.consolidate_memory)"
python -m venv "$W/v14" && "$W/v14/bin/pip" install -q "openminion==0.1.14" > "$OUT/pip-0.1.14.log" 2>&1
"$W/v14/bin/python" -I -c "import openminion, sys; print('0.1.14 venv:', openminion.__version__, sys.version.split()[0])" | tee -a "$T"
"$PY" -I -c "import openminion, sys; print('checkout:', openminion.__version__, sys.version.split()[0])" | tee -a "$T"
git log --oneline -1 | tee -a "$T"

########################################################################## home A
A="$W/homeA"
note "home A: a consolidation job created by 0.1.14; replaced before it runs on this package"
make_home "$A" | tee -a "$T"
run "$A" "$W/v14/bin/openminion" tools run task.consolidate_memory --json '{"interval_hours": 12, "batch_limit": 8}'
OLD_A="$(jget tool.data.task_id 2>/dev/null || jget task_id 2>/dev/null)"
echo "[setup] legacy task in A: $OLD_A" | tee -a "$T"
task_kind "$A" "$OLD_A"

note "A. inspect: list the tasks and show the consolidation task"
run "$A" "$OM" tasks list --json
summary
run "$A" "$OM" tasks show "$OLD_A" --json
run "$A" "$OM" tools run task.list --json '{"limit": 50}'

note "A. pause the old job before creating its replacement"
run "$A" "$OM" tasks pause "$OLD_A" --json
run "$A" "$OM" tools run task.consolidate_memory --json '{"interval_hours": 12, "batch_limit": 8}'
NEW_A="$(jget tool.data.task_id 2>/dev/null || jget task_id 2>/dev/null)"
echo "[replacement in A: $NEW_A]" | tee -a "$T"
task_kind "$A" "$NEW_A"
note "A. verify: only the replacement is enabled"
run "$A" "$OM" tasks list --json
summary

note "A. the replacement runs as a consolidation turn under the daemon"
due_now "$A" "$NEW_A"
run "$A" "$OM" daemon start
wait_last_run "$A" "$NEW_A" finished
run "$A" "$OM" tasks show "$NEW_A" --json
run "$A" "$OM" daemon stop
run "$A" "$OM" tasks list --json
summary

########################################################################## home B
B="$W/homeB"
note "home B: a consolidation job created by 0.1.14 that runs once on this package"
make_home "$B" | tee -a "$T"
run "$B" "$W/v14/bin/openminion" tools run task.consolidate_memory --json '{"interval_hours": 6, "batch_limit": 5}'
OLD_B="$(jget tool.data.task_id 2>/dev/null || jget task_id 2>/dev/null)"
echo "[setup] legacy task in B: $OLD_B" | tee -a "$T"
due_now "$B" "$OLD_B"
run "$B" "$OM" daemon start
wait_last_run "$B" "$OLD_B" failed
run "$B" "$OM" daemon stop

note "B. inspect the paused job"
run "$B" "$OM" tasks list --json
summary
run "$B" "$OM" tasks show "$OLD_B" --json
run "$B" "$OM" tools run task.list --json '{"limit": 50}'

note "B. resume only re-enables it (observation)"
run "$B" "$OM" tasks resume "$OLD_B" --json
run "$B" "$OM" tasks list --json
summary

note "B. cancel the old job, then create its replacement"
run "$B" "$OM" tasks cancel "$OLD_B" --json
run "$B" "$OM" tools run task.consolidate_memory --json '{"interval_hours": 6, "batch_limit": 5}'
NEW_B="$(jget tool.data.task_id 2>/dev/null || jget task_id 2>/dev/null)"
echo "[replacement in B: $NEW_B]" | tee -a "$T"
task_kind "$B" "$NEW_B"
note "B. verify: only the replacement is enabled"
run "$B" "$OM" tasks list --json
summary

########################################################################## raw cron API
note "raw cron API: creating a job with the reserved payload is rejected"
(cd "$B" && "$OM" --home-root "$B" --data-root "$B/.openminion" --config "$B/agents.json" --no-update-check \
   api run --host 127.0.0.1 --port 18766) > "$W/api.out" 2>&1 &
API_PID=$!
for _ in $(seq 1 60); do grep -q "listening" "$W/api.out" 2>/dev/null && break; sleep 1; done
sed -e "s#$W#<work>#g" "$W/api.out" | tee -a "$T"
BODY='{"name": "raw consolidation", "agent_id": "openminion", "schedule": {"kind": "every", "every_ms": 43200000}, "payload": {"kind": "agentTurn", "message": "review memory candidates", "_openminion_memory_consolidation": {"target_scope": "agent:openminion"}}}'
printf '\n$ curl -sS -X POST http://127.0.0.1:18766/v1/cron/jobs -H "Content-Type: application/json" -d %q\n' "$BODY" | tee -a "$T"
curl -sS -X POST http://127.0.0.1:18766/v1/cron/jobs -H "Content-Type: application/json" -d "$BODY" -w '\n[http %{http_code}]\n' | tee -a "$T"
printf '\n$ curl -sS http://127.0.0.1:18766/v1/cron/jobs\n' | tee -a "$T"
curl -sS http://127.0.0.1:18766/v1/cron/jobs -w '\n[http %{http_code}]\n' | head -c 4000 | tee -a "$T"
kill "$API_PID" 2>/dev/null; wait "$API_PID" 2>/dev/null
run "$B" "$OM" tasks list --json
summary

note "done"
