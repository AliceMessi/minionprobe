#!/bin/bash
# Linux gates for #408: the 12 tests and the issue's six targets on the branch
# head and on dev's tests, test_migrations_runner.py with PostgreSQL, the
# storagectl restore probe, ruff, format, workflow, lint and test-critical.
set -u
EV=/tmp/ev
PY=.venv/bin/python3.11
DEV=b670bbb6
T12=$(tr '\n' ' ' < $EV/tests12.txt)
SIX="tests/test_storage_migrations.py tests/storage/test_migrations_runner.py tests/storage/test_migration_telemetry.py tests/storage/test_module_ops.py tests/storage/test_storagectl_cli.py tests/test_api_runtime.py::APIRuntimeBootstrapTests"
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($PY -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
sha256sum $EV/probe_storagectl_restore.py $EV/run408.sh
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- tests CHANGELOG.md; sec "dev's tests ($DEV) from here on: git checkout $DEV -- tests"; git status --short | head; fi
  for i in 1 2 3; do
    sec "[$tree] the 12 tests of #408, run $i: pytest -q -p no:cacheprovider -rf --tb=short <12 ids>"
    PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -rf --tb=short $T12 2>&1 | tail -4
  done
  sec "[$tree] the six targets of #408: pytest -q -p no:cacheprovider -rf --tb=short $SIX"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -rf --tb=short $SIX 2>&1 | tail -4
  sec "[$tree] tests/storage/test_migrations_runner.py with PostgreSQL (module-level postgres marker; make test-ci deselects it): pytest -q -m postgres"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -rf --tb=short -m postgres tests/storage/test_migrations_runner.py 2>&1 | tail -4
done
git checkout -q HEAD -- tests CHANGELOG.md; sec "back to the branch head"; git status --short | head
sec "storagectl restore probe (src is unchanged by the branch): PYTHONPATH=src:. python probe_storagectl_restore.py"
PYTHONPATH=src:. $PY $EV/probe_storagectl_restore.py 2>&1
sec "ruff check ."; $PY -m ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make workflow-check"; make workflow-check 2>&1 | tail -4
sec "make lint"; make lint 2>&1 | tail -40; echo "make lint exit: ${PIPESTATUS[0]}"
sec "make test-critical"; make test-critical 2>&1 | tail -3
