#!/bin/bash
# #495 PR gates on the runner (--postgres): the report's probe, the new tests, #469's
# room tests, the upstream postgres-memory-retrieval job, a related test-ci slice and
# the repo gates on the branch head; then the probe, the new and #469's tests and the
# slice again on dev sources (git checkout $DEV -- src).
# The dispatched command writes probes/probe_selection_leave_race.py to $PROBE first.
set -u
DEV="37fa75ffcc6ccb02c896c89a067414c34dba55eb"
P=.venv/bin/python
PROBE=/tmp/probe/probe_selection_leave_race.py
RMARK="not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow"
RELATED="tests/test_session_store.py tests/test_room_cli.py tests/test_turn_router.py tests/cli/interactive/test_openminion_runtime.py tests/services/runtime/test_session_profile_binding.py tests/api/routes/test_session_continuations.py tests/session tests/storage"
# The new SQLite tests, and #469's one-after-another scenario that now also checks a
# refused selection of the agent that left.
NEW_SQLITE=(tests/test_session_store.py -k "selection_waits_and_then_applies or selection_during_a_leave or legacy_session_keeps_its_selection_rules or joins_selection_and_leaves_on_two_connections")
NEW_PG=(-m postgres tests/integration/test_runtime_storage_postgres.py -k "leave-during-selection or selection-during-leave or one-after-another or legacy_session_keeps_its_selection_rules")
# #469's room tests on SQLite (#364, #385 and #393 scenarios included), as in its PR,
# and the whole PostgreSQL file, which holds #469's PostgreSQL room tests.
ROOM_SQLITE=(tests/test_session_store.py -k "active_agent or selection or leave or legacy or blank or join")
ROOM_PG=(-m postgres tests/integration/test_runtime_storage_postgres.py)
sec() { echo; echo "== $*"; }
pyt() { $P -m pytest -q -p no:cacheprovider -rfE --tb=short "$@" 2>&1 | grep -E "^(FAILED|ERROR|E  )|passed|failed|error|AssertionError" | sed -E 's/^(.{300}).*/\1 .../'; echo "pytest exit=${PIPESTATUS[0]}"; }
pyv() { $P -m pytest -v -p no:cacheprovider -rfE --tb=line "$@" 2>&1 | grep -E "PASSED|FAILED|ERROR|passed|failed|AssertionError|^/" | sed -E 's/^(.{300}).*/\1 .../'; echo "pytest exit=${PIPESTATUS[0]}"; }
probe_runs() {  # $1 = tree label; three runs
  sec "[$1] probe sha256: $(sha256sum $PROBE | cut -d' ' -f1); openminion from: $(PYTHONPATH=src $P -c 'import openminion; print(openminion.__file__)')"
  for run in 1 2 3; do
    sec "[$1] probe run $run"
    PYTHONPATH=src $P $PROBE 2> /tmp/probe-stderr.txt; echo "probe exit=$?"
    echo "(stderr: $(wc -l < /tmp/probe-stderr.txt) lines, $(grep -c 'has pending migrations' /tmp/probe-stderr.txt) of them the PostgreSQL storage's 'has pending migrations' warning)"
    grep -v 'has pending migrations' /tmp/probe-stderr.txt | head -5
  done
}
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "commits on dev:"; git log --oneline $DEV..HEAD
echo "files changed by the test commit (HEAD~2) against dev:"; git diff --stat $DEV HEAD~2 | sed 's/^/  /'
echo "files changed by the fix and docs commits:"; git diff --stat HEAD~2 HEAD | sed 's/^/  /'
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($P -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|psycopg-binary|pytest|ruff) ' | tr -s ' ' | tr '\n' ';')"
echo "PostgreSQL server: $($P -c 'import os,sqlalchemy as s;e=s.create_engine(os.environ["OPENMINION_TEST_POSTGRES_URL"]);c=e.connect();print(c.exec_driver_sql("show server_version").scalar());c.close()' 2>&1 | tail -1)"
for tree in head dev; do
  if [ $tree = dev ]; then
    git checkout -q $DEV -- src
    sec "dev sources from here on: git checkout $DEV -- src ($(git status --short -- src | wc -l) paths differ from the head; src identical to dev: $(git diff --quiet $DEV -- src && echo yes || echo no))"
  fi
  probe_runs $tree
  sec "[$tree] new tests, SQLite"; pyv "${NEW_SQLITE[@]}"
  sec "[$tree] new tests, PostgreSQL"; pyv "${NEW_PG[@]}"
  sec "[$tree] #469's room tests, SQLite: tests/test_session_store.py -k 'active_agent or selection or leave or legacy or blank or join'"; pyt "${ROOM_SQLITE[@]}"
  sec "[$tree] the PostgreSQL file: -m postgres tests/integration/test_runtime_storage_postgres.py"; pyt "${ROOM_PG[@]}"
  sec "[$tree] pytest -m \"<test-ci markers>\" <related>"; pyt -m "$RMARK" $RELATED
  if [ $tree = head ]; then
    sec "[head] new tests 10 times on each backend (last pytest line per run, counted)"
    for i in $(seq 1 10); do $P -m pytest -q -p no:cacheprovider "${NEW_SQLITE[@]}" 2>&1 | tail -1 | sed -E 's/ in [0-9.]+s.*//'; done | sort | uniq -c | sed 's/^/SQLite: /'
    for i in $(seq 1 10); do $P -m pytest -q -p no:cacheprovider "${NEW_PG[@]}" 2>&1 | tail -1 | sed -E 's/ in [0-9.]+s.*//'; done | sort | uniq -c | sed 's/^/PostgreSQL: /'
    sec "[head] upstream postgres-memory-retrieval job, its command list from .github/workflows/ci.yml"
    sed -n '/Run PostgreSQL memory, skill and runtime storage conformance/,/^  [a-z]/p' .github/workflows/ci.yml | grep -E "python -m pytest" | sed -E 's/^ +//' | while read -r line; do echo "\$ $line"; eval "${line/python/$P}" 2>&1 | tail -1; done
    sec "ruff check ."; .venv/bin/ruff check . 2>&1 | tail -3; echo "ruff exit=${PIPESTATUS[0]}"
    sec "make format-check"; make format-check 2>&1 | tail -3; echo "make format-check exit=${PIPESTATUS[0]}"
    sec "make lint"; make lint 2>&1 | tail -14; echo "make lint exit=${PIPESTATUS[0]}"
  fi
done
git checkout -q HEAD -- src; sec "back to the head: $(git status --short -- src | wc -l) paths differ"
