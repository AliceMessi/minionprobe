#!/bin/bash
# #496 PR gates on the runner: lint, the upstream postgres-memory-retrieval job,
# the focused and related tests on the branch head, on the #471 head's sources
# (git checkout $BASE -- src) and the new #496 tests on dev's sources, then
# ruff and format checks.
set -u
BASE="6064d6d78dc2fee872c581805ca53dd84cca0026"   # PR #471 head (fix/448-capture-authorization)
DEV="ce3b677173e0f344d85a729113e04cf708fa6587"    # dev
N="tests/runtime/test_turn_optional_memory_writes.py"
NEW="$N tests/brain/test_decision_rationale_memory.py tests/brain/test_runner_tools.py tests/memory/runtime/test_capture_authorization.py tests/runtime/test_turn_capture_authorization.py"
F="$NEW tests/memory/runtime/test_capture_recovery.py tests/runtime/test_cron_capture_recovery.py tests/memory/test_external_backend_capture.py tests/integration/test_brain_auto_fact_extraction_coverage.py tests/brain/test_auto_fact_extraction.py tests/services/brain/test_post_execution.py tests/services/agent/test_execution_resources_memory.py tests/services/agent/test_execution_policy.py tests/test_agent_check_command.py tests/test_doctor_command.py"
R="tests/memory tests/runtime tests/brain tests/services tests/integration tests/api tests/identity tests/policy tests/tools/memory tests/modules/tool tests/test_agent_check_command.py tests/test_doctor_command.py"
RMARK="not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow"
P=.venv/bin/python
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "base: $(git log --oneline -1 $BASE)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
pyt() { $P -m pytest -q -p no:cacheprovider -rfE --tb=line "$@" 2>&1 | grep -E "^FAILED|^ERROR|passed|failed|error" | sed -E 's/^(.{300}).*/\1 .../'; }
sec "make lint"; make lint 2>&1 | tail -14; echo "make lint exit: ${PIPESTATUS[0]}"
sec "python -m scripts.validate.services_layout"; $P -m scripts.validate.services_layout 2>&1 | head -2
sec "[head] upstream postgres-memory-retrieval job, its command list from .github/workflows/ci.yml"
sed -n '/Run PostgreSQL memory, skill and runtime storage conformance/,/^  [a-z]/p' .github/workflows/ci.yml | grep -E "python -m pytest" | sed -E 's/^ +//' | while read -r line; do echo "\$ $line"; eval "${line/python/$P}" 2>&1 | tail -1; done
for tree in head base; do
  if [ $tree = base ]; then git checkout -q $BASE -- src; sec "#471 head sources from here on: git checkout $BASE -- src ($(git status --short | grep -v '^?? .deps' | wc -l) paths differ)"; fi
  sec "[$tree] pytest <focused>"; pyt $F
  sec "[$tree] pytest -m \"<test-ci markers>\" <related>"; pyt -m "$RMARK" $R
done
git checkout -q $DEV -- src; sec "dev sources: git checkout $DEV -- src ($(git status --short | grep -v '^?? .deps' | wc -l) paths differ)"
sec "[dev] pytest $N"; pyt $N
git checkout -q HEAD -- src; sec "back to the branch head: $(git status --short | grep -v '^?? .deps' | wc -l) paths differ"
sec "[head] new and changed tests, 5 runs"; for i in 1 2 3 4 5; do pyt $NEW; done
sec "ruff check ."; .venv/bin/ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
