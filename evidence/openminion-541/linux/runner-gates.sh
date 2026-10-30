#!/bin/bash
# Refs #541 gates on the runner: lint and layout validators, the focused and
# related tests on the branch head, the new test file 3 times, the installed
# wheel SDK typing check from ci.yml, then the new and changed tests on dev's
# sources (git checkout $DEV -- src), ruff and format.
set -u
DEV="1169bd226b428f9187e2b5652a01ab7f29757271"   # upstream dev the branch starts from
N="tests/runtime/test_agent_explicit_thread.py"
NEW="$N tests/api/test_agent.py"
F="$NEW tests/api/test_api_layout_validator.py tests/runtime/test_turn_capture_authorization.py tests/runtime/test_turn_denied_call_outcome.py tests/runtime/test_turn_optional_memory_writes.py tests/api/test_agent_sessions.py tests/api/test_tool_controls.py tests/api/test_public_surface.py tests/api/test_turn_api_runtime_facade.py"
R="tests/api tests/runtime tests/brain tests/services tests/session tests/policy"
RMARK="not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow"
P=.venv/bin/python
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; git log --oneline -5 | sed "s/^/  /"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
pyt() { $P -m pytest -q -p no:cacheprovider -rfE --tb=line "$@" 2>&1 | grep -E "^FAILED|^ERROR|passed|failed|error" | sed -E 's/^(.{300}).*/\1 .../'; }
sec "git diff --stat $DEV..HEAD"; git diff --stat $DEV..HEAD | tail -12
sec "CHANGELOG: sections around the #541 entry"; grep -n "^## \|(Refs #541)" CHANGELOG.md | head -4
sec "make lint"; make lint > /tmp/lint.log 2>&1; echo "make lint exit: $?"; tail -12 /tmp/lint.log
echo "-- lint lines with FAIL or Error:"; grep -nE "FAIL|Error|error:" /tmp/lint.log | head -20
sec "python scripts/validate/max_file_loc.py"; $P scripts/validate/max_file_loc.py 2>&1 | tail -3; echo "max_file_loc exit: ${PIPESTATUS[0]}"
sec "python -m scripts.validate.api_layout"; $P -m scripts.validate.api_layout 2>&1 | tail -4; echo "api_layout exit: ${PIPESTATUS[0]}"
sec "python -m scripts.validate.cli_layout"; $P -m scripts.validate.cli_layout 2>&1 | tail -3; echo "cli_layout exit: ${PIPESTATUS[0]}"
sec "[head] the new file, verbose"; $P -m pytest -q -p no:cacheprovider -v $N 2>&1 | grep -E "PASSED|FAILED|ERROR|passed|failed" | sed -E 's/ +\[ *[0-9]+%\]$//'
sec "[head] new file, 3 runs"; for i in 1 2 3; do pyt $N; done
sec "[head] pytest <focused>"; pyt $F
sec "[head] pytest -m \"<test-ci markers>\" <related>"; pyt -m "$RMARK" $R
sec "[head] installed wheel SDK typing (the ci.yml step)"
(
  set -e
  sdk_root="$(mktemp -d)"; mkdir -p "$sdk_root/dist" "$sdk_root/consumer"
  $P -m build --wheel --outdir "$sdk_root/dist" > "$sdk_root/build.log" 2>&1
  python -m venv "$sdk_root/venv"
  "$sdk_root/venv/bin/python" -m pip install -q "$sdk_root"/dist/openminion-*.whl 'mypy>=1.10,<2' > "$sdk_root/pip.log" 2>&1
  cp tests/typing/fixtures/sdk_consumer_valid.py tests/typing/fixtures/sdk_consumer_invalid.py "$sdk_root/consumer/"
  cd "$sdk_root/consumer"
  echo "wheel: $(ls "$sdk_root"/dist)"
  PYTHONPATH='' "$sdk_root/venv/bin/python" -m mypy --strict sdk_consumer_valid.py
  set +e
  out="$(PYTHONPATH='' "$sdk_root/venv/bin/python" -m mypy --strict --show-error-codes sdk_consumer_invalid.py 2>&1)"; st=$?
  echo "invalid fixture: exit $st, $(printf '%s\n' "$out" | grep -c ': error:') error(s)"
  printf '%s\n' "$out" | grep -F 'Argument 1 to "run" of "Agent" has incompatible type "int"; expected "str"  [arg-type]' > /dev/null && echo "invalid fixture: expected arg-type error found"
); echo "sdk typing exit: $?"
git checkout -q $DEV -- src; sec "dev sources: git checkout $DEV -- src ($(git status --short | grep -v '^?? .deps' | wc -l) paths differ)"
sec "[dev] pytest <new and changed>, verbose"; $P -m pytest -q -p no:cacheprovider -v -rfE --tb=line $NEW 2>&1 | grep -E "PASSED|FAILED|ERROR|^E |passed|failed|error" | grep -v "^tests/api/test_agent.py::.* PASSED" | sed -E 's/ +\[ *[0-9]+%\]$//; s/^(.{300}).*/\1 .../'
sec "[dev] pytest <focused>"; pyt $F
git checkout -q HEAD -- src; sec "back to the branch head: $(git status --short | grep -v '^?? .deps' | wc -l) paths differ"
sec "ruff check ."; .venv/bin/ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2; echo "format-check exit: ${PIPESTATUS[0]}"
