#!/usr/bin/env bash
# #456 follow-up gates, run from the branch checkout on the Linux runner after `make dev-install`:
#   bash gates_456.sh <out_dir> <transcript_script>
set -u -o pipefail
OUT="$(mkdir -p "$1" && cd "$1" && pwd)"
TRANSCRIPT_SCRIPT="$2"
PY=.venv/bin/python
MARKERS="not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow"
F=tests/runtime/test_cron_capture_recovery.py
NEW="$F::test_daemon_runs_a_task_consolidate_memory_job_with_its_persisted_task_kind $F::test_daemon_run_of_a_task_consolidate_memory_job_keeps_unsupported_work_pending $F::test_daemon_pauses_a_pre_upgrade_consolidation_job_and_keeps_its_pending_work"
FOCUSED="$F tests/runtime/test_cron_turn_executor.py tests/api/test_cron_task_ownership.py tests/services/test_runtime_ingress_contract.py tests/services/gateway/test_gateway_utils.py tests/runtime/test_manager.py tests/tools/task/test_plugin.py tests/task/test_operational_controls.py tests/runtime/test_turn_capture_authorization.py tests/memory/runtime/test_capture_authorization.py tests/runtime/test_cron_scheduler.py tests/test_daemon_resilience.py tests/cli/test_cli_layout_validator.py tests/e2e/test_memory_consolidation_coordination.py"
RELATED="tests/runtime tests/task tests/tools/task tests/cron tests/api tests/services tests/memory tests/cli tests/test_daemon_resilience.py"

step() { printf '\n=== %s\n' "$*"; }

# mutate FILE OLD NEW: replace the one occurrence of OLD (fails if it is not exactly one).
mutate() {
  "$PY" -I - "$@" <<'EOF'
import sys
path, old, new = sys.argv[1:4]
text = open(path, encoding="utf-8").read()
assert text.count(old) == 1, (path, text.count(old))
open(path, "w", encoding="utf-8").write(text.replace(old, new))
EOF
}

step "head"
git log --oneline -3
git status --short
"$PY" -c 'import sys, sqlite3; print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'
. /etc/os-release && echo "$PRETTY_NAME"

step "focused: pytest -q <focused>"
"$PY" -m pytest -q -p no:cacheprovider $FOCUSED 2>&1 | tail -8

step "new tests, 5 runs"
for i in 1 2 3 4 5; do "$PY" -m pytest -q -p no:cacheprovider $NEW 2>&1 | tail -1; done

step "new tests -vv (durations)"
"$PY" -m pytest -q -p no:cacheprovider -vv --durations=0 $NEW 2>&1 | grep -E "PASSED|FAILED|ERROR|s call|passed|failed"

# The #463 provenance, disabled in this checkout only (never committed), one at a time.
run_mutation() {
  local name="$1"
  step "mutation $name: git diff"
  git diff -- src
  step "mutation $name: pytest -q <new tests>"
  "$PY" -m pytest -q -p no:cacheprovider -rf $NEW 2>&1 | grep -E "^(FAILED|ERROR)|passed|failed|^E  " | head -40
  step "mutation $name: pytest -q $F (whole file)"
  "$PY" -m pytest -q -p no:cacheprovider $F 2>&1 | tail -1
  git checkout -- src
  git status --short
}
mutate src/openminion/tools/task/plugin.py $'            metadata={"kind": CONSOLIDATION_TASK_KIND},\n' ''
run_mutation "M1 (task.consolidate_memory stores no task kind)"
mutate src/openminion/services/runtime/daemon.py $'        timeout_s=cron_timeout_s,\n        task_manager=task_manager,\n' $'        timeout_s=cron_timeout_s,\n        task_manager=None,\n'
run_mutation "M2 (the daemon's CronTurnExecutor gets no task manager)"
mutate src/openminion/services/runtime/cron/executor.py 'if task is None or task.metadata.get("kind") != CONSOLIDATION_TASK_KIND:' 'if False:  # mutation: provenance check disabled'
run_mutation "M3 (the executor's task-kind check is disabled)"

step "CLI documentation validator: python -m scripts.validate.cli_layout"
"$PY" -m scripts.validate.cli_layout; echo "exit=$?"

step "related: pytest -q -m \"<test-ci markers>\" $RELATED"
"$PY" -m pytest -q -p no:cacheprovider -m "$MARKERS" $RELATED 2>&1 | tail -15

step "ruff check ."
"$PY" -m ruff check . 2>&1 | tail -3
step "make format-check"
make format-check 2>&1 | tail -3
step "make lint"
make lint 2>&1 | tail -40; echo "make lint exit=${PIPESTATUS[0]}"

step "transcript: bash transcript_456.sh"
timeout 2400 bash "$TRANSCRIPT_SCRIPT" "$OUT/transcript"; echo "transcript exit=$?"
grep -E "^\[summary\]|^\[wait\]|^\[evidence\]|^\[http|^\[replacement|^\[exit [1-9]" "$OUT/transcript/transcript.txt"
step "done"
