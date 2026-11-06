#!/bin/bash
# Published-source verification of #581 (PR #627) and #582 (PR #628) on the release tag.
# Runs from the checkout of openminion/openminion at the tag (make dev-install, postgres extra,
# PostgreSQL 16 service). Steps: tag identity, release_equivalence.py (bytes, executable AST,
# docstrings/comments, test names/fixtures/assertions), affected-file py_compile + ruff,
# collected test ids on the pre-cleanup trees vs the tag, the focused tests at the tag
# (PostgreSQL ones included), and the upstream postgres-memory-retrieval job at the tag.
set -u
TOOLS="$(dirname "$0")"
PRE581=a754ed20ee5a49eff4178ea4e920cc851c57f50f; M581=42475bbdc993914310fa80027f093b0b5f512428
PRE582=e103e93c04a34cb1a7298e20aec89501bba1ad9c; M582=b7e6fb302b4984a2c67b61c2cfade63cdbc417d1
P=.venv/bin/python
sec() { echo; echo "== $*"; }
git fetch -q --unshallow origin 2>/dev/null || git fetch -q origin
git fetch -q origin 'refs/tags/v0.1.19:refs/tags/v0.1.19' 2>/dev/null || true
echo "checkout: $(git rev-parse HEAD)  tag v0.1.19 -> $(git rev-parse 'v0.1.19^{commit}') (tag object $(git rev-parse v0.1.19))"
echo "describe: $(git describe --tags --exact-match HEAD 2>&1)"
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "openminion (editable checkout): $($P -c 'import openminion;print(openminion.__version__)')"
echo "ruff: $(.venv/bin/ruff --version)"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
echo "postgres: $($P - <<'PY'
import os
import psycopg
url = os.environ["OPENMINION_TEST_POSTGRES_URL"].replace("postgresql+psycopg://", "postgresql://")
with psycopg.connect(url) as c:
    print(c.execute("select version()").fetchone()[0])
PY
)"
for m in $M581 $M582; do git merge-base --is-ancestor $m HEAD && echo "merge $m is an ancestor of the tag"; done

FILES581="$(git diff --name-only $PRE581 $M581 | tr '\n' ' ')"
FILES582="$(git diff --name-only $PRE582 $M582 | tr '\n' ' ')"
PY581="$(for f in $FILES581; do case $f in *.py) echo $f;; esac; done | tr '\n' ' ')"
PY582="$(for f in $FILES582; do case $f in *.py) echo $f;; esac; done | tr '\n' ' ')"
# Focused: the touched test modules, the PR-time list of tests importing a touched module, and any
# test file at the tag that imports a touched helper module (re-derived, in case a later PR added one).
importers() { for f in "$@"; do case $(basename $f) in _*.py) m=$(basename $f .py); grep -rlE "tests\.$m\b|from tests import $m\b" tests --include='test_*.py';; esac; done; }
F581="$( { for f in $PY581; do case $(basename $f) in test_*) echo $f;; esac; done; echo tests/test_conversation_lifecycle.py tests/test_run_status.py tests/test_session_metadata_filters.py tests/test_session_store.py tests/test_storage_migrations.py tests/integration/test_runtime_storage_postgres.py tests/integration/test_module_baselines_postgres.py tests/storage/test_migrations_runner.py tests/memory/test_postgres_capture_bundle.py | tr ' ' '\n'; importers $PY581; } | sort -u | tr '\n' ' ')"
F582="$( { for f in $PY582; do case $(basename $f) in test_*) echo $f;; esac; done; echo tests/api/test_runtime_storage_connection.py tests/integration/test_runtime_memory_lifecycle.py tests/integration/test_runtime_memory_postgres.py tests/integration/test_runtime_storage_postgres.py tests/memory/test_gc_audited_store.py tests/memory/test_gc_evidence.py tests/memory/test_gc_evidence_postgres.py tests/memory/test_gc_postgres.py tests/runtime/test_research_usage_boundary.py tests/runtime/test_whole_turn_usage.py | tr ' ' '\n'; importers $PY582; } | sort -u | tr '\n' ' ')"
echo "#581 paths: $(echo $FILES581 | wc -w), focused test files: $(echo $F581 | wc -w)"
echo "#582 paths: $(echo $FILES582 | wc -w), focused test files: $(echo $F582 | wc -w)"

sec "release_equivalence.py (pre-cleanup, merged, tag)"
$P -I "$TOOLS/release_equivalence.py" . HEAD 581:$PRE581:$M581 582:$PRE582:$M582 > /tmp/eq.txt; echo "exit $?"
grep -E "^(tag|python|######## |== |   tag bytes|   executable AST|   merged docstrings|   merged comments|   tests |   top-level|     tag only|     merged only|   changed after|     [0-9a-f]{8} |#58[12] summary|RESULT)" /tmp/eq.txt

sec "py_compile + ruff on the 26 touched .py files at the tag"
$P -m py_compile $PY581 $PY582 && echo "py_compile ok ($(echo $PY581 $PY582 | wc -w) files)"
.venv/bin/ruff check $PY581 $PY582; .venv/bin/ruff format --check $PY581 $PY582

collect() { $P -m pytest -q -p no:cacheprovider --collect-only "$@" 2>/dev/null | grep '::' | sort; }
for spec in "581 $PRE581" "582 $PRE582"; do
  set -- $spec; n=$1; pre=$2; eval "F=\$F$n"
  sec "#$n collected test ids: pre-cleanup tree ($pre) vs tag"
  collect $F > /tmp/ids-tag-$n.txt
  git checkout -q $pre -- src tests
  collect $F > /tmp/ids-pre-$n.txt
  git checkout -q HEAD -- src tests
  echo "tag ids: $(wc -l < /tmp/ids-tag-$n.txt) (sha256 $(sha256sum < /tmp/ids-tag-$n.txt | cut -c1-16)), pre ids: $(wc -l < /tmp/ids-pre-$n.txt) (sha256 $(sha256sum < /tmp/ids-pre-$n.txt | cut -c1-16))"
  if diff /tmp/ids-pre-$n.txt /tmp/ids-tag-$n.txt > /tmp/ids-diff-$n.txt; then echo "collected ids identical"; else echo "collected ids differ:"; cat /tmp/ids-diff-$n.txt; fi
  echo "tree back at the tag: $(git status --short -- src tests | wc -l) paths differ"
done

pyt() { $P -m pytest -q -p no:cacheprovider -rfE --tb=line "$@" 2>&1 | grep -E "^FAILED|^ERROR|passed|failed|error|skipped" | sed -E 's/^(.{240}).*/\1 .../'; }
sec "[tag] #581 focused tests (OPENMINION_TEST_POSTGRES_URL set)"; pyt $F581
sec "[tag] #582 focused tests (OPENMINION_TEST_POSTGRES_URL set)"; pyt $F582
sec "[tag] upstream postgres-memory-retrieval job, its command list from .github/workflows/ci.yml"
sed -n '/Run PostgreSQL memory, skill and runtime storage conformance/,/^  [a-z]/p' .github/workflows/ci.yml | grep -E "python -m pytest" | sed -E 's/^ +//' | while read -r line; do echo "\$ $line"; eval "${line/python/$P}" 2>&1 | tail -1; done
sec "done"
