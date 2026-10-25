#!/bin/bash
# Linux gates for #400 (PostgreSQL 16 service, OPENMINION_TEST_POSTGRES_URL set).
set -u
EV=/tmp/ev
PY=.venv/bin/python3.11
DEV=8b220e14
FOCUSED="tests/memory/test_gc_evidence.py tests/memory/test_gc_evidence_postgres.py tests/architecture/test_marker_discipline.py"
RELATED="tests/memory/test_gc.py tests/memory/test_gc_postgres.py tests/memory/test_mii_phase3_lifecycle.py tests/memory/test_session_memory_durability.py tests/memory/test_store_characterization.py tests/integration/test_mii_phase3_aging.py tests/services/agent/test_continuity_hardening.py tests/services/agent/test_truth_maintenance.py"
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($PY -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($PY -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|sophiagraph) ' | tr -s ' ' | tr '\n' ';')"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
sha256sum $EV/*.py $EV/run400.sh
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; sec "dev sources from here on: git checkout $DEV -- src"; git status --short | grep -v '^?? .deps' | head; fi
  sec "[$tree] pytest -q -p no:cacheprovider -rf --tb=line <focused>"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -rf --tb=line $FOCUSED 2>&1 | tail -14
  sec "[$tree] the postgres-memory-retrieval job's commands, each with -m postgres"
  for t in tests/memory/test_memory_postgres_conformance.py tests/memory/test_memory_search_quality.py::test_postgres_lexical_score_contributes_to_semantic_order tests/memory/test_gc_evidence_postgres.py tests/skill/test_skill_postgres_conformance.py tests/integration/test_runtime_storage_postgres.py tests/integration/test_runtime_memory_postgres.py; do
    echo "-- $t: $(PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -m postgres $t 2>&1 | tail -1)"
  done
  sec "[$tree] pytest -q -p no:cacheprovider -rf --tb=line <related>"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -rf --tb=line $RELATED 2>&1 | tail -6
  sec "[$tree] the issue's probe: PYTHONPATH=src:. python probe_pg_evidence_refs.py"
  PYTHONPATH=src:. $PY $EV/probe_pg_evidence_refs.py 2>&1
done
git checkout -q HEAD -- src; sec "back to the branch head"; git status --short | head
sec "composition with PR #399: merge pull/399/head into the branch head (CHANGELOG.md conflict resolved with the branch side; no other conflict)"
git fetch -q https://github.com/openminion/openminion.git pull/399/head && P399=$(git rev-parse FETCH_HEAD) && echo "PR #399 head: $(git log --oneline -1 $P399)"
git -c user.name=runner -c user.email=runner@example.invalid merge --no-commit --no-ff $P399 2>&1 | tail -4
git checkout --ours CHANGELOG.md && git add CHANGELOG.md; git status --short | grep -v '^?? .deps' | grep -v '^M ' | head
echo "-- with #399 and #400:"; PYTHONPATH=src:. $PY $EV/compose399.py 2>&1
git checkout -q $P399 -- src/openminion/modules/memory/runtime/purge.py
echo "-- with #399 only (purge.py from #399's head):"; PYTHONPATH=src:. $PY $EV/compose399.py 2>&1
git checkout -q HEAD -- src/openminion/modules/memory/runtime/purge.py
echo "-- #399 and #400 PostgreSQL tests on the merge: $(PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -m postgres tests/memory/test_gc_postgres.py tests/memory/test_gc_evidence_postgres.py 2>&1 | tail -1)"
git merge --abort; git checkout -q HEAD -- . ; sec "merge aborted"; git status --short | head
sec "ruff check ."; $PY -m ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make workflow-check"; make workflow-check 2>&1 | tail -4
sec "make lint"; make lint 2>&1 | tail -16; echo "make lint exit: ${PIPESTATUS[0]}"
sec "make test-critical"; make test-critical 2>&1 | tail -3
