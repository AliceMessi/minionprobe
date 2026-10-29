#!/bin/bash
# #493 PR gates on the runner (preset openminion, --postgres). On the branch head:
# make lint, the upstream postgres-memory-retrieval job's commands (read from
# .github/workflows/ci.yml), the focused tests, the PostgreSQL test file and a
# related test-ci slice, ruff, format-check and test-critical. The same tests again
# after `git checkout $DEV -- src`. Then the #493 report's probe, unchanged, on the
# head and on dev sources. Full pytest and probe outputs go to $RUNNER_TEMP/logs.
set -u
DEV="37fa75ffcc6ccb02c896c89a067414c34dba55eb"
F="tests/storage/test_module_alembic_onboarding.py tests/cli/test_storage_migration_commands.py tests/cli/test_storage_migrate_report_fields.py tests/tool/authoring/test_storage_foundation.py tests/storage/test_migrations_runner.py"
PG="tests/integration/test_storage_migrate_report_postgres.py"
R="tests/storage tests/cli tests/tool/authoring"
RMARK="not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow"
PROBE_URL="https://raw.githubusercontent.com/AliceMessi/minionprobe/main/evidence/openminion-compress-alembic-config/probes/probe_compress_alembic_config.py"
REPO="$(pwd)"
P="$REPO/.venv/bin/python"
LOGS="$RUNNER_TEMP/logs"
mkdir -p "$LOGS/pytest" "$LOGS/probe"
sec() { echo; echo "== $*"; }
# pyt <log name> <pytest args>: full output to $LOGS/pytest/<log name>.txt; FAILED/ERROR lines and the summary here.
pyt() {
  local name="$1"; shift
  $P -m pytest -q -p no:cacheprovider -rfE --tb=short "$@" > "$LOGS/pytest/$name.txt" 2>&1
  local rc=$?
  grep -E "^(FAILED|ERROR) " "$LOGS/pytest/$name.txt" | sed -E 's/^(.{300}).*/\1 .../'
  tail -1 "$LOGS/pytest/$name.txt"; echo "exit=$rc"
}
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($P -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|alembic|pytest|ruff|mypy) ' | tr -s ' ' | tr '\n' ';')"
echo "postgres: $(docker exec pg psql -U postgres -tAc 'select version()' 2>/dev/null | cut -c1-40)"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
sec "git diff --stat $DEV HEAD"; git diff --stat $DEV HEAD
sec "make lint"; make lint > "$LOGS/make-lint.txt" 2>&1; rc=$?; tail -14 "$LOGS/make-lint.txt"; echo "make lint exit: $rc"
sec "[head] upstream postgres-memory-retrieval job, its command list from .github/workflows/ci.yml"
i=0
sed -n '/Run PostgreSQL memory, skill and runtime storage conformance/,/^  [a-z]/p' .github/workflows/ci.yml | grep -E "python -m pytest" | sed -E 's/^ +//' | while read -r line; do
  i=$((i+1)); echo "\$ $line"; eval "${line/python/$P}" < /dev/null > "$LOGS/pytest/pg-job-$i.txt" 2>&1; rc=$?; echo "  $(tail -1 "$LOGS/pytest/pg-job-$i.txt") (exit=$rc)"
done
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; sec "dev sources from here on: git checkout $DEV -- src ($(git diff --name-only HEAD -- src | wc -l) path(s) differ: $(git diff --name-only HEAD -- src | tr '\n' ' '))"; fi
  sec "[$tree] pytest <focused>"; pyt "$tree-focused" $F
  sec "[$tree] pytest -m postgres $PG"; pyt "$tree-postgres" -m postgres $PG
  sec "[$tree] pytest -m \"<test-ci markers>\" $R"; pyt "$tree-related" -m "$RMARK" $R
done
git checkout -q HEAD -- src; sec "back to the branch head: $(git status --short -- src | wc -l) path(s) differ under src"
sec "ruff check ."; .venv/bin/ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make test-critical"; make test-critical > "$LOGS/make-test-critical.txt" 2>&1; rc=$?; tail -2 "$LOGS/make-test-critical.txt"; echo "make test-critical exit: $rc"
sec "#493 report probe, unchanged: $PROBE_URL"
mkdir -p "$RUNNER_TEMP/probe"
curl -fsSL "$PROBE_URL" -o "$RUNNER_TEMP/probe/probe_compress_alembic_config.py"
(cd "$RUNNER_TEMP/probe" && sha256sum probe_compress_alembic_config.py) | tee "$LOGS/probe/probe-sha256.txt"
for tree in pr dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; fi
  echo "-- $tree: src at $([ $tree = dev ] && echo "$DEV (git checkout $DEV -- src)" || git rev-parse HEAD)"
  mkdir -p "$RUNNER_TEMP/empty-$tree"
  (cd "$RUNNER_TEMP/empty-$tree" && env -u OPENMINION_HOME -u OPENMINION_DATA_ROOT PGURL="$OPENMINION_TEST_POSTGRES_URL" \
    "$P" -I -X utf8 "$RUNNER_TEMP/probe/probe_compress_alembic_config.py" "$LOGS/probe" "$tree" \
    > "$LOGS/probe/probe_compress_alembic_config_$tree.log" 2>&1; echo "probe exit=$?" >> "$LOGS/probe/probe_compress_alembic_config_$tree.log")
  grep -E "^CASE|exit=|summary:|success=|file after|database after|warning: .*compress|probe exit" "$LOGS/probe/probe_compress_alembic_config_$tree.log" | grep -v "pg_full_run" | sed -E 's/^(.{300}).*/\1 .../'
done
git checkout -q HEAD -- src; echo; echo "final: $(git status --short -- src | wc -l) path(s) differ under src"
