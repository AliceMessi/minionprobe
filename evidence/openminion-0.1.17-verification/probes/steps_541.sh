# #541 on the runner (sourced by linux_setup.sh from an empty directory; run with
# --need-0116 for the control). probe_541.py with the published 0.1.16 (the
# control, when PY0116 is set), then with the package under test ($PY, version
# $OM_VERSION) and the control's JSON as the baseline; then the consumer typing
# check (steps_541_typing.sh) in new venvs holding openminion==$OM_VERSION (and
# ==0.1.16) plus mypy>=1.10,<2 from PyPI. docs_runtime-surfaces.md is the
# published docs/runtime-surfaces.md at v0.1.17 (blob 358abd59).
set +e
WK="$RUNNER_TEMP/work"
DOC="$B/docs_runtime-surfaces.md"
sha256sum "$DOC" > "$OUT/doc-sha256.txt"
run() {  # run <name> <python> <args...>
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL |^SUMMARY|^case .*error=[^N]" "$OUT/$name.log" | cut -c1-300
}
base=()
if [[ -n "${PY0116:-}" ]]; then
  run probe_541_0116 "$PY0116" "$B/probe_541.py" "$WK/541-0116" "$OUT/probe_541_0116.json" --doc "$DOC" --expect-version 0.1.16
  base=(--baseline "$OUT/probe_541_0116.json")
fi
run probe_541 "$PY" "$B/probe_541.py" "$WK/541" "$OUT/probe_541.json" --doc "$DOC" --expect-version "$OM_VERSION" "${base[@]}"
echo "empty dir after probes: $(ls -A "$EMPTY" | wc -l) entries"
# shellcheck source=/dev/null
source "$B/steps_541_typing.sh"
uv venv -q --python 3.12 "$RUNNER_TEMP/venv541t"
uv pip install -q --python "$RUNNER_TEMP/venv541t/bin/python" --index-url https://pypi.org/simple "openminion==$OM_VERSION" 'mypy>=1.10,<2'
uv pip show --python "$RUNNER_TEMP/venv541t/bin/python" openminion mypy > "$OUT/typing-pip-show.txt" 2>&1
typing_541 "$RUNNER_TEMP/venv541t/bin/python" "$B" "$WK/541-typing" "$OUT/typing" fixed
echo "typing exit=$?" | tee -a "$OUT/typing/typing-summary.txt"
if [[ -n "${PY0116:-}" ]]; then
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv541t16"
  uv pip install -q --python "$RUNNER_TEMP/venv541t16/bin/python" --index-url https://pypi.org/simple "openminion==0.1.16" 'mypy>=1.10,<2'
  uv pip show --python "$RUNNER_TEMP/venv541t16/bin/python" openminion mypy > "$OUT/typing-pip-show-0.1.16.txt" 2>&1
  typing_541 "$RUNNER_TEMP/venv541t16/bin/python" "$B" "$WK/541-typing-0116" "$OUT/typing-0116" control
  echo "typing 0.1.16 exit=$?" | tee -a "$OUT/typing-0116/typing-summary.txt"
fi
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
