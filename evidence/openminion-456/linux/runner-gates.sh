#!/bin/bash
set -u
DEV="39c36a1b5183ca811a9031cbcb397b3f9470a06e"
TESTC="988503a5d84aefa31a3aec9bc2c236023161cc3c"
F="tests/runtime/test_cron_consolidation_turn.py tests/services/test_runtime_ingress_contract.py tests/runtime/test_cron_turn_executor.py tests/runtime/test_cron_capture_recovery.py tests/e2e/test_memory_consolidation_coordination.py tests/runtime/test_manager_ingress_characterization.py tests/services/gateway/test_gateway_utils.py"
RM="tests/runtime tests/services tests/task tests/tools/task tests/api tests/routines tests/brain/test_proactive_autonomous_entrypoint.py tests/integration/test_lgmh_smoke.py tests/test_daemon.py tests/test_daemon_resilience.py"
RMARK="not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow"
P=.venv/bin/python; DEV=${DEV:-a41487ab1b51f7fd526ff29cd526f0b98dc01fc7}
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($P -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|sophiagraph|typer) ' | tr -s ' ' | tr '\n' ';')"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
mkdir -p /tmp/ev
pyt() { $P -m pytest -q -p no:cacheprovider -rfE --tb=line "$@" 2>&1 | grep -E "^FAILED|^ERROR|passed|failed|error" | sed -E 's/^(.{240}).*/\1 .../'; }
for tree in head dev; do
  if [ $tree = dev ]; then
    git checkout -q $DEV -- src; git checkout -q $TESTC -- tests
    sec "dev sources and test-commit tests from here on: git checkout $DEV -- src; git checkout $TESTC -- tests ($(git status --short | grep -v '^?? .deps' | wc -l) paths differ)"
  fi
  sec "[$tree] pytest <focused>"; pyt $F
  sec "[$tree] pytest -m \"$RMARK\" <related>"; pyt -m "$RMARK" $RM
done
git checkout -q HEAD -- src tests; sec "back to the branch head: $(git status --short | grep -v '^?? .deps' | wc -l) paths differ"
sec "[head] tests/runtime/test_cron_consolidation_turn.py, 10 more runs"
for i in 1 2 3 4 5 6 7 8 9 10; do pyt tests/runtime/test_cron_consolidation_turn.py | tail -1; done
sec "ruff check ."; .venv/bin/ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make workflow-check"; make workflow-check 2>&1 | tail -4
sec "make lint"; make lint 2>&1 | tail -40; echo "make lint exit: ${PIPESTATUS[0]}"
sec "make test-critical"; make test-critical 2>&1 | tail -3
