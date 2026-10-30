#!/bin/bash
# #534 (Refs #524) round-2 gates on the runner: lint, the upstream
# postgres-memory-retrieval job, the focused and related tests on the branch
# head, the new and changed tests on dev's sources (git checkout $DEV -- src),
# then ruff, format and the CLI-docs validator.
set -u
DEV="f7b6100ae06f326399942fed04e5f5f95e5f70b3"   # dev after the 0.1.16 release
N="tests/runtime/test_turn_denied_call_outcome.py"
NEW="$N tests/runtime/test_turn_optional_memory_writes.py tests/brain/test_runner_tools.py"
F="$NEW tests/runtime/test_turn_capture_authorization.py tests/brain/test_command_executor.py tests/brain/test_decision_rationale_memory.py tests/memory/runtime/test_capture_authorization.py tests/memory/test_tool_failure_diagnostics.py tests/integration/test_mii_phase2_cross_session.py tests/services/agent/test_memory_consolidation.py tests/brain/test_commerce_handoff_result.py"
R="tests/brain tests/runtime tests/memory tests/services tests/integration tests/api tests/policy tests/tools/memory tests/modules/tool"
RMARK="not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow"
P=.venv/bin/python
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; git log --oneline -5 | sed "s/^/  /"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
pyt() { $P -m pytest -q -p no:cacheprovider -rfE --tb=line "$@" 2>&1 | grep -E "^FAILED|^ERROR|passed|failed|error" | sed -E 's/^(.{300}).*/\1 .../'; }
sec "git diff --stat $DEV..HEAD"; git diff --stat $DEV..HEAD | tail -12
sec "CHANGELOG: sections around the #524 entry"; grep -n "^## \|(Refs #524)" CHANGELOG.md | head -5
sec "make lint"; make lint 2>&1 | tail -14; echo "make lint exit: ${PIPESTATUS[0]}"
sec "python scripts/validate/max_file_loc.py"; $P scripts/validate/max_file_loc.py 2>&1 | tail -3
sec "python -m scripts.validate.cli_layout"; $P -m scripts.validate.cli_layout 2>&1 | tail -3; echo "cli_layout exit: ${PIPESTATUS[0]}"
sec "[head] upstream postgres-memory-retrieval job, its command list from .github/workflows/ci.yml"
sed -n '/Run PostgreSQL memory, skill and runtime storage conformance/,/^  [a-z]/p' .github/workflows/ci.yml | grep -E "python -m pytest" | sed -E 's/^ +//' | while read -r line; do echo "\$ $line"; eval "${line/python/$P}" 2>&1 | tail -1; done
sec "[head] the takeover control, verbose"; $P -m pytest -q -p no:cacheprovider -v "tests/brain/test_runner_tools.py::test_executed_takeover_result_is_policy_denied_and_stages_no_tool_outcome" 2>&1 | grep -E "PASSED|FAILED|passed|failed"
sec "[head] pytest <focused>"; pyt $F
sec "[head] pytest -m \"<test-ci markers>\" <related>"; pyt -m "$RMARK" $R
sec "[head] new and changed tests, 3 runs"; for i in 1 2 3; do pyt $NEW; done
git checkout -q $DEV -- src; sec "dev sources: git checkout $DEV -- src ($(git status --short | grep -v '^?? .deps' | wc -l) paths differ)"
sec "[dev] pytest <new and changed>"; $P -m pytest -q -p no:cacheprovider -rfE --tb=short $NEW 2>&1 | grep -E "^FAILED|^ERROR|^E  |passed|failed|error" | sed -E 's/^(.{400}).*/\1 .../'
sec "[dev] pytest <focused>"; pyt $F
git checkout -q HEAD -- src; sec "back to the branch head: $(git status --short | grep -v '^?? .deps' | wc -l) paths differ"
sec "ruff check ."; .venv/bin/ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
