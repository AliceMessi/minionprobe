#!/bin/bash
# Linux gates for #402 (PostgreSQL 16 service, OPENMINION_TEST_POSTGRES_URL set).
set -u
EV=/tmp/ev
PY=.venv/bin/python3.11
DEV=b670bbb6
FOCUSED="tests/memory/test_operability.py"
RELATED="tests/integration/test_operability.py tests/memory/test_operability_postgres.py tests/memory/test_memory_blocks_cli.py tests/memory/test_audit_trail.py"
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($PY -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($PY -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|sophiagraph|typer) ' | tr -s ' ' | tr '\n' ';')"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
sha256sum $EV/*.py $EV/run402.sh
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; sec "dev sources from here on: git checkout $DEV -- src"; git status --short | grep -v '^?? .deps' | head; fi
  sec "[$tree] pytest -q -p no:cacheprovider -rf --tb=short <focused>"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -rf --tb=short $FOCUSED 2>&1 | grep -E "^E  |FAILED|passed|failed" | head -20
  sec "[$tree] pytest -q -p no:cacheprovider -rf --tb=line <related> (PostgreSQL cases included)"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -rf --tb=line $RELATED 2>&1 | tail -4
  sec "[$tree] the issue's probe: PYTHONPATH=src:. python probe_inspect_history.py"
  PYTHONPATH=src:. $PY $EV/probe_inspect_history.py 2>&1 | sed "s#$(pwd)#<checkout>#g"
  sec "[$tree] PostgreSQL inspect history (unchanged, SQLite-only summary): PYTHONPATH=src:. python pg_inspect_history.py"
  PYTHONPATH=src:. $PY $EV/pg_inspect_history.py 2>&1
  sec "[$tree] memctl inspect scope handling (separate report): PYTHONPATH=src:. python probe_inspect_scope.py"
  PYTHONPATH=src:. $PY $EV/probe_inspect_scope.py 2>&1
done
git checkout -q HEAD -- src; sec "back to the branch head"; git status --short | head
sec "ruff check ."; $PY -m ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make workflow-check"; make workflow-check 2>&1 | tail -4
sec "make lint"; make lint 2>&1 | tail -16; echo "make lint exit: ${PIPESTATUS[0]}"
sec "make test-critical"; make test-critical 2>&1 | tail -3
