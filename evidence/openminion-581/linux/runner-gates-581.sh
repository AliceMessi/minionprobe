#!/bin/bash
# #581 commentary PR gates on the runner: equivalence, affected-file syntax and Ruff,
# collected test ids on dev and head, the focused tests (SQLite and PostgreSQL),
# the upstream postgres-memory-retrieval job, then ruff check ., make format-check, make lint.
set -u
DEV="83d97ad33f9b34332c97ed2b2e88ef3cf68c8b6f"
TOOLS="$(dirname "$0")"
FILES="src/openminion/modules/storage/migrations/postgres_schema.py src/openminion/modules/storage/runtime/session_store/metadata_strings.py src/openminion/modules/storage/runtime/session_store/run_facts.py tests/integration/test_postgres_module_schemas.py tests/storage/test_postgres_schema.py tests/_room_participant_scenarios.py tests/_session_metadata_filter_scenarios.py tests/_session_metadata_strings_scenarios.py tests/_run_status_scenarios.py"
# The touched test files, and every test file that imports a touched scenario or source module.
F="tests/storage/test_postgres_schema.py tests/integration/test_postgres_module_schemas.py tests/test_conversation_lifecycle.py tests/test_run_status.py tests/test_session_metadata_filters.py tests/test_session_store.py tests/test_storage_migrations.py tests/integration/test_runtime_storage_postgres.py tests/integration/test_module_baselines_postgres.py tests/storage/test_migrations_runner.py tests/memory/test_postgres_capture_bundle.py"
P=.venv/bin/python
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "ruff: $(.venv/bin/ruff --version)"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
pyt() { $P -m pytest -q -p no:cacheprovider -rfE --tb=line "$@" 2>&1 | grep -E "^FAILED|^ERROR|passed|failed|error|skipped" | sed -E 's/^(.{240}).*/\1 .../'; }
sec "git diff --stat $DEV HEAD"; git diff --stat $DEV HEAD
sec "AST equivalence, docstrings stripped (dev vs head)"; $P -I "$TOOLS/ast_equivalence.py" . $DEV HEAD | grep -E "^(base|head|python|==|   ast|   docstrings changed|files:)"
sec "syntax: py_compile of the affected files"; $P -m py_compile $FILES && echo "py_compile ok ($(echo $FILES | wc -w) files)"
sec "ruff check <affected>"; .venv/bin/ruff check $FILES
sec "ruff format --check <affected>"; .venv/bin/ruff format --check $FILES
sec "collected test ids, dev vs head"
$P -m pytest -q -p no:cacheprovider --collect-only $F 2>/dev/null | grep '::' | sort > /tmp/ids-head.txt
git checkout -q $DEV -- src tests docs
$P -m pytest -q -p no:cacheprovider --collect-only $F 2>/dev/null | grep '::' | sort > /tmp/ids-dev.txt
git checkout -q HEAD -- src tests docs
echo "head ids: $(wc -l < /tmp/ids-head.txt), dev ids: $(wc -l < /tmp/ids-dev.txt), sha256 head $(sha256sum < /tmp/ids-head.txt | cut -c1-16), dev $(sha256sum < /tmp/ids-dev.txt | cut -c1-16)"
diff /tmp/ids-dev.txt /tmp/ids-head.txt && echo "collected ids identical"
echo "tree back at head: $(git status --short -- src tests docs | wc -l) paths differ"
sec "[head] pytest <focused> (OPENMINION_TEST_POSTGRES_URL set, so the postgres-marked ones run too)"; pyt $F
sec "[head] upstream postgres-memory-retrieval job, its command list from .github/workflows/ci.yml"
sed -n '/Run PostgreSQL memory, skill and runtime storage conformance/,/^  [a-z]/p' .github/workflows/ci.yml | grep -E "python -m pytest" | sed -E 's/^ +//' | while read -r line; do echo "\$ $line"; eval "${line/python/$P}" 2>&1 | tail -1; done
sec "ruff check ."; .venv/bin/ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2; echo "make format-check exit: ${PIPESTATUS[0]}"
sec "make lint"; make lint 2>&1 | tail -16; echo "make lint exit: ${PIPESTATUS[0]}"
