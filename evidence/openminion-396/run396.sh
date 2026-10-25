#!/bin/bash
# Linux gates for #396 (PostgreSQL 16 service, OPENMINION_TEST_POSTGRES_URL set).
set -u
EV=/tmp/ev
PY=.venv/bin/python3.11
DEV=b670bbb6
FOCUSED="tests/test_run_status.py tests/integration/test_runtime_storage_postgres.py tests/test_session_store.py tests/test_conversation_lifecycle.py tests/architecture/test_marker_discipline.py"
RELATED="tests/api/test_agent_sessions.py tests/api/test_provider_failure.py tests/api/test_runtime_error_run_state.py tests/api/test_turn_deadline.py tests/services/gateway/test_invocation_start_lifecycle.py tests/task/test_autonomy.py tests/test_api_correlation.py tests/test_api_runs.py tests/cli/test_autonomy_command.py"
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($PY -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($PY -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|sophiagraph) ' | tr -s ' ' | tr '\n' ';')"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
echo "PostgreSQL: $($PY - <<'EOF' 2>&1
import os, sqlalchemy as sa
e = sa.create_engine(os.environ["OPENMINION_TEST_POSTGRES_URL"])
with e.connect() as c:
    print(c.execute(sa.text("SHOW server_version")).scalar(), "| lc_collate", c.execute(sa.text("SHOW lc_collate")).scalar())
EOF
)"
sha256sum $EV/*.py $EV/run396.sh
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; sec "dev sources from here on: git checkout $DEV -- src"; git status --short | grep -v '^?? .deps' | head; fi
  sec "[$tree] pytest -q -p no:cacheprovider -rf --tb=line <focused>"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -rf --tb=line $FOCUSED 2>&1 | tail -14
  sec "[$tree] the PostgreSQL tests, with -m postgres: tests/integration/test_runtime_storage_postgres.py (the postgres-memory-retrieval job's command)"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -m postgres -rf --tb=line tests/integration/test_runtime_storage_postgres.py 2>&1 | tail -4
  echo "-- the new case test alone: $(PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -m postgres tests/integration/test_runtime_storage_postgres.py::test_postgres_run_readers_match_the_run_prefix_case_sensitively 2>&1 | tail -1)"
  sec "[$tree] pytest -q -p no:cacheprovider -rf --tb=line <related>"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -rf --tb=line $RELATED 2>&1 | tail -6
  sec "[$tree] the issue's probe: PYTHONPATH=src:. python probe_run_event_type_case.py <work_dir>"
  PYTHONPATH=src:. $PY $EV/probe_run_event_type_case.py /tmp/probe-$tree 2>&1
  sec "[$tree] query plans: PYTHONPATH=src:. python plans_396.py <work_dir>"
  PYTHONPATH=src:. $PY $EV/plans_396.py /tmp/plans-$tree 2>&1
done
git checkout -q HEAD -- src; sec "back to the branch head"; git status --short | grep -v '^?? .deps' | head
sec "ruff check ."; $PY -m ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make workflow-check"; make workflow-check 2>&1 | tail -4
sec "make lint"; make lint 2>&1 | tail -16; echo "make lint exit: ${PIPESTATUS[0]}"
sec "make test-critical"; make test-critical 2>&1 | tail -3
