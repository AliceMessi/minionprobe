#!/usr/bin/env bash
# #456 follow-up, round 2: the guidance's scope on an upgrade from 0.1.15 or later.
#
# Run from the openminion checkout on the Linux runner (after `make dev-install`):
#   bash transcript_456r2.sh <out_dir>
#
# One runtime home E with two consolidation jobs, upgraded 0.1.14 -> 0.1.16 -> this checkout:
# - job X created with openminion==0.1.14's CLI (no task kind) and never replaced;
# - job Y created with openminion==0.1.16's CLI (trusted kind) after the home was opened by 0.1.16.
# Then this checkout's daemon runs both. Expected from the guidance: Y keeps running
# as consolidation (not a pre-upgrade job, not to be replaced); X fails with the exact
# provenance message and is paused (the diagnostic), and only X is replaced.
# Setup lines are marked [setup]; [evidence] lines read the task database directly
# (the CLI does not show the task kind). Every `$ openminion ...` line is the checkout's
# own CLI unless it says openminion(0.1.14) or openminion(0.1.16).
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
  [[ "$bin" == "$W/v16/bin/openminion" ]] && shown="openminion(0.1.16)"
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

note "setup: openminion==0.1.14 and openminion==0.1.16 from PyPI, each in a separate venv"
for v in 14 16; do
  python -m venv "$W/v$v" && "$W/v$v/bin/pip" install -q "openminion==0.1.$v" > "$OUT/pip-0.1.$v.log" 2>&1
  "$W/v$v/bin/pip" freeze > "$OUT/pip-freeze-0.1.$v.txt"
  "$W/v$v/bin/python" -I -c "import openminion, sys; print('[setup] 0.1.$v venv: openminion', openminion.__version__, 'CPython', sys.version.split()[0])" | tee -a "$T"
done
"$PY" -I -c "import openminion, sqlite3, sys; print('[setup] checkout: openminion', openminion.__version__, 'CPython', sys.version.split()[0], 'SQLite', sqlite3.sqlite_version)" | tee -a "$T"
echo "[setup] checkout head: $(git log --oneline -1)" | tee -a "$T"

########################################################################## home E
E="$W/homeE"
note "E: X from 0.1.14 (never replaced), Y from 0.1.16; this checkout's daemon runs both"
make_home "$E" openminion
run "$E" "$W/v14/bin/openminion" tools run task.consolidate_memory --json '{"interval_hours": 12, "batch_limit": 8}'
X="$(created_id)"
echo "[setup] job X (0.1.14) in E: $X" | tee -a "$T"
run "$E" "$W/v16/bin/openminion" tools run task.consolidate_memory --json '{"interval_hours": 6, "batch_limit": 5}'
Y="$(created_id)"
echo "[setup] job Y (0.1.16) in E: $Y" | tee -a "$T"
task_kind "$E" "$X"
task_kind "$E" "$Y"

note "E. inspect with this checkout's CLI before any run"
run "$E" "$OM" tasks list --json
summary

note "E. this checkout's daemon runs both jobs"
due_now "$E" "$X"
due_now "$E" "$Y"
run "$E" "$OM" daemon start
wait_new_run "$E" "$Y" ""
wait_new_run "$E" "$X" ""
read -r FIRST_Y _ <<< "$(last_run_id "$E" "$Y")"
run "$E" "$OM" tasks show "$X" --json
run "$E" "$OM" tasks show "$Y" --json
run "$E" "$OM" tasks list --json
summary

note "E. Y keeps running: its next scheduled run is a consolidation run too"
due_now "$E" "$Y"
wait_new_run "$E" "$Y" "$FIRST_Y"

note "E. replace only X (the job whose run failed with the message)"
run "$E" "$OM" tasks cancel "$X" --json
run "$E" "$OM" tools run task.consolidate_memory --json '{"interval_hours": 12, "batch_limit": 8}'
Z="$(created_id)"
echo "[replacement of X in E: $Z]" | tee -a "$T"
task_kind "$E" "$Z"
run "$E" "$OM" daemon stop
run "$E" "$OM" tasks list --json
summary
task_kind "$E" "$Y"

note "done"
