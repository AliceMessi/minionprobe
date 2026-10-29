#!/bin/bash
# #494 PR gates on the runner (openminion preset, --postgres): the focused tests,
# the upstream postgres-memory-retrieval job, related PostgreSQL and test-ci tests
# and the SQLite shipped-vs-branch probe on the branch head; the focused tests with
# only the baseline fix (git checkout $BASEFIX -- src); the same tests on dev
# sources (git checkout $DEV -- src); then ruff, format-check and make lint on head.
set -u
DEV="37fa75ffcc6ccb02c896c89a067414c34dba55eb"
BASEFIX="2ef06cce4568ff60742a21574a4dba1965747d3e"
F="tests/integration/test_module_baselines_postgres.py tests/integration/test_storage_migrations_applied.py"
PGX="tests/controlplane/test_controlplane_postgres_conformance.py tests/telemetry/test_telemetry_postgres_conformance.py tests/controlplane/test_postgres_dispatcher_roundtrip.py tests/integration/test_postgres_tier1_promotion.py tests/integration/test_postgres_tier2_promotion.py"
R="tests/storage tests/controlplane tests/telemetry tests/cli/test_storage_migration_commands.py tests/cli/test_storage_migrate_report_fields.py tests/integration/test_storage_migrations_applied.py"
RMARK="not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow"
P=.venv/bin/python
OUT="$RUNNER_TEMP/logs/out"
mkdir -p "$OUT"
sec() { echo; echo "== $*"; }
pyt() { $P -m pytest -q -p no:cacheprovider -rfE --tb=line "$@" 2>&1 | grep -E "^FAILED|^ERROR|passed|failed|error" | sed -E 's/^(.{400}).*/\1 .../'; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"; echo "baseline fix only: $(git log --oneline -1 $BASEFIX)"
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($P -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|alembic) ' | tr -s ' ' | tr '\n' ';')"
echo "postgres: $(docker exec pg psql -U postgres -tAc 'show server_version' 2>/dev/null)"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
sec "[head] pytest <focused>"; pyt $F
sec "[head] upstream postgres-memory-retrieval job, its command list from .github/workflows/ci.yml"
sed -n '/Run PostgreSQL memory, skill and runtime storage conformance/,/^  [a-z]/p' .github/workflows/ci.yml | grep -E "python -m pytest" | sed -E 's/^ +//' | while read -r line; do echo "\$ $line"; eval "${line/python/$P}" 2>&1 | tail -1; done
sec "[head] pytest -m postgres <postgres related>"; pyt -m postgres $PGX
sec "[head] pytest -m \"<test-ci markers>\" <related>"; pyt -m "$RMARK" $R
sec "[head] SQLite: dev $DEV migration scripts against the branch's"
mkdir -p "$RUNNER_TEMP/dev"
git archive "$DEV" src/openminion/modules/controlplane/storage src/openminion/modules/telemetry/storage | tar -x -C "$RUNNER_TEMP/dev"
sha256sum "$RUNNER_TEMP/probe_494_sqlite_shipped_vs_head.py" | sed "s#$RUNNER_TEMP/##"
(cd "$RUNNER_TEMP" && "$OLDPWD/$P" -I probe_494_sqlite_shipped_vs_head.py "$RUNNER_TEMP/dev" "$OUT")
git checkout -q $BASEFIX -- src
sec "[baseline fix only] git checkout $BASEFIX -- src ($(git status --short -- src | wc -l) paths differ from head); pytest <focused>"; pyt $F
git checkout -q $DEV -- src
sec "[dev sources] git checkout $DEV -- src ($(git status --short -- src | wc -l) paths differ from head)"
sec "[dev] pytest <focused>"; pyt $F
sec "[dev] pytest -m postgres <postgres related>"; pyt -m postgres $PGX
sec "[dev] pytest -m \"<test-ci markers>\" <related>"; pyt -m "$RMARK" $R
git checkout -q HEAD -- src; sec "back to the branch head: $(git status --short -- src | wc -l) paths differ"
sec "ruff check ."; .venv/bin/ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make lint"; make lint 2>&1 | tail -14; echo "make lint exit: ${PIPESTATUS[0]}"
