#!/bin/bash
# Linux gates for #403 (PostgreSQL 16 service, OPENMINION_TEST_POSTGRES_URL set).
set -u
EV=/tmp/ev
PY=.venv/bin/python3.11
DEV=b670bbb6
FOCUSED="tests/memory/test_memctl_service_close.py tests/memory/test_cli_read_commands.py tests/memory/test_memory_review_cli.py tests/memory/test_utility_plan_cli.py tests/memory/test_operability.py tests/memory/test_operability_postgres.py tests/architecture/test_marker_discipline.py"
RELATED="tests/memory/test_bundle_provenance_roundtrip.py tests/memory/test_cli_provenance_and_forget.py tests/memory/test_memory_blocks_cli.py tests/memory/test_tool_failure_diagnostics.py tests/integration/test_memory_export_import.py tests/integration/test_operability.py tests/memory/test_audit_trail.py"
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($PY -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($PY -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|typer|click) ' | tr -s ' ' | tr '\n' ';')"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
sha256sum $EV/*.py $EV/run403.sh
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src scripts/baselines; sec "dev sources from here on: git checkout $DEV -- src scripts/baselines"; git status --short | grep -v '^?? .deps' | head; fi
  sec "[$tree] pytest -q -p no:cacheprovider -rf --tb=line <focused>"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -rf --tb=line $FOCUSED 2>&1 | tail -16
  sec "[$tree] the PostgreSQL memctl operability tests, with -m postgres"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -m postgres -rf --tb=line tests/memory/test_operability_postgres.py 2>&1 | tail -3
  sec "[$tree] pytest -q -p no:cacheprovider -rf --tb=line <related>"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -rf --tb=line $RELATED 2>&1 | tail -6
  sec "[$tree] the issue's probe: PYTHONPATH=src:. python probe_memctl_audit_sink.py"
  PYTHONPATH=src:. $PY $EV/probe_memctl_audit_sink.py 2>&1
done
git checkout -q HEAD -- src scripts/baselines; sec "back to the branch head"; git status --short | grep -v '^?? .deps' | head
sec "ruff check ."; $PY -m ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make workflow-check"; make workflow-check 2>&1 | tail -4
sec "make lint"; make lint 2>&1 | tail -16; echo "make lint exit: ${PIPESTATUS[0]}"
sec "make test-critical"; make test-critical 2>&1 | tail -3
