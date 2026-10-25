#!/bin/bash
# Linux gates for #397 (PostgreSQL 16 service, OPENMINION_TEST_POSTGRES_URL set).
set -u
EV=/tmp/ev
PY=.venv/bin/python3.11
DEV=b670bbb6
FOCUSED="tests/memory/test_external_backend_capture.py tests/memory/test_backend_interfaces_kce01.py tests/memory/test_external_backend_contract_kce09.py tests/runtime/test_bootstrap_memory_retrieve_di.py tests/memory/runtime/test_capture_recovery.py tests/architecture/test_marker_discipline.py"
RELATED="tests/brain/test_cross_turn_goal_persistence.py tests/services/gateway/test_gateway_memory.py tests/memory/test_postgres_capture_bundle.py tests/services/runtime/test_memory_assembly.py tests/brain/test_memory_contract_adapters.py"
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($PY -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($PY -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|sophiagraph) ' | tr -s ' ' | tr '\n' ';')"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
sha256sum $EV/*.py $EV/run397.sh
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; sec "dev sources from here on: git checkout $DEV -- src"; git status --short | grep -v '^?? .deps' | head; fi
  sec "[$tree] pytest -q -p no:cacheprovider -rf --tb=line <focused>"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -rf --tb=line -p no:logging $FOCUSED 2>&1 | grep -v "^20[0-9][0-9]-" | tail -12
  sec "[$tree] pytest -q -p no:cacheprovider -rf --tb=line <related>"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -rf --tb=line -p no:logging $RELATED 2>&1 | grep -v "^20[0-9][0-9]-" | tail -6
  sec "[$tree] the PostgreSQL capture bundle tests, with -m postgres"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -m postgres -rf --tb=line tests/memory/test_postgres_capture_bundle.py tests/memory/test_memory_postgres_conformance.py 2>&1 | tail -3
  sec "[$tree] the issue's probe: PYTHONPATH=src:. python probe_external_capture.py"
  PYTHONPATH=src:. $PY $EV/probe_external_capture.py 2>&1 | grep -v "^20[0-9][0-9]-"
done
git checkout -q HEAD -- src; sec "back to the branch head"; git status --short | grep -v '^?? .deps' | head
sec "composition with PR #398: merge pull/398/head into the branch head"
git fetch -q https://github.com/openminion/openminion.git pull/398/head && P398=$(git rev-parse FETCH_HEAD) && echo "PR #398 head: $(git log --oneline -1 $P398)"
git -c user.name=runner -c user.email=runner@example.invalid merge --no-commit --no-ff $P398 2>&1 | tail -4
echo "-- unmerged paths: $(git diff --name-only --diff-filter=U | tr '\n' ' ')"
echo "-- <focused> on the merge: $(PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -p no:logging $FOCUSED 2>&1 | tail -1)"
echo "-- #398's tests on the merge: $(PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -p no:logging tests/services/runtime/test_memory_assembly.py tests/memory/test_backend_interfaces_kce01.py tests/api/test_artifact_runtime_roots.py 2>&1 | tail -1)"
echo "-- #398's PostgreSQL tests on the merge (-m postgres): $(PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -m postgres tests/integration/test_runtime_memory_postgres.py 2>&1 | tail -1)"
echo "-- the issue's probe on the merge:"; PYTHONPATH=src:. $PY $EV/probe_external_capture.py 2>&1 | grep -v "^20[0-9][0-9]-" | grep -E '"(raised|run_state|capture (intents|results).*)":'
git merge --abort; git checkout -q HEAD -- . ; sec "merge aborted"; git status --short | grep -v '^?? .deps' | head
sec "ruff check ."; $PY -m ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make workflow-check"; make workflow-check 2>&1 | tail -4
sec "make lint"; make lint 2>&1 | tail -16; echo "make lint exit: ${PIPESTATUS[0]}"
sec "make test-critical"; make test-critical 2>&1 | tail -3
