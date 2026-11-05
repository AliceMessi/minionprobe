# #456 on the runner (sourced by linux_setup.sh from an empty directory; NEED_0116=1 for the controls).
# The package under test is $PY ($OM_VERSION). Older published packages from PyPI in their own venvs:
#   0.1.14 (creates the pre-0.1.15 jobs; installed here), 0.1.16 ($PY0116, or installed here for the 0.1.18
#   re-run: the mixed-age home's job and the negative control).
# The published guide: docs/runtime-database-upgrades.md fetched from the tag v0.1.17 on GitHub (docs/ is not
# in the wheel); its sha256 and git blob are recorded, and the probes check the blob again.
# The workflow input limit needs two bundles; this file runs the probes that are in the bundle:
#   bundle 1: probe_456          new jobs, ordinary and forged-metadata controls (0.1.15 round's probe, byte copy)
#             probe_456_intents  pending capture intents through the guide's cancel/pause/replacement
#   bundle 2: probe_456_guide    the guide's commands with the real CLI and the real daemon, homes A-F
set +e
WK="$RUNNER_TEMP/work"
mkdir -p "$WK"
uv venv -q --python 3.12 "$RUNNER_TEMP/venv0114"
uv pip install -q --python "$RUNNER_TEMP/venv0114/bin/python" --index-url https://pypi.org/simple "openminion==0.1.14"
PY0114="$RUNNER_TEMP/venv0114/bin/python"
uv pip show --python "$PY0114" openminion > "$OUT/pip-show-0.1.14.txt"
if [[ -z "${PY0116:-}" ]]; then
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv0116"
  uv pip install -q --python "$RUNNER_TEMP/venv0116/bin/python" --index-url https://pypi.org/simple "openminion==0.1.16"
  uv pip show --python "$RUNNER_TEMP/venv0116/bin/python" openminion > "$OUT/pip-show-0.1.16.txt"
  MIXED16="$RUNNER_TEMP/venv0116/bin"; CONTROL=0
else
  MIXED16="$(dirname "$PY0116")"; CONTROL=1
fi
GUIDE="$WK/runtime-database-upgrades.md"
curl -fsSL https://raw.githubusercontent.com/openminion/openminion/v0.1.17/docs/runtime-database-upgrades.md -o "$GUIDE"
{ echo "source: https://raw.githubusercontent.com/openminion/openminion/v0.1.17/docs/runtime-database-upgrades.md"
  sha256sum "$GUIDE" | sed "s#$WK/##"; git hash-object "$GUIDE" | sed 's/^/git blob: /'; } > "$OUT/guide-check.txt" 2>&1
cat "$OUT/guide-check.txt"
run() {  # run <name> <python> <args...>
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL |^SUMMARY" "$OUT/$name.log" | cut -c1-240
}
BIN="$(dirname "$PY")"; BIN14="$(dirname "$PY0114")"
if [[ -f "$B/probe_456_intents.py" ]]; then
  run probe_456 "$PY" "$B/probe_456.py" "$WK/456" "$OUT/probe_456.json"
  run probe_456_intents_create "$PY0114" "$B/probe_456_intents.py" create "$WK/int" "$OUT/probe_456_intents_create.json"
  run probe_456_intents "$PY" "$B/probe_456_intents.py" upgrade "$WK/int" "$OUT/probe_456_intents.json" "$GUIDE" /bin/bash
  if [[ "$CONTROL" == "1" ]]; then
    # negative control: the published 0.1.16 as the package under test, then 0.1.14 (before #463)
    run probe_456_0116 "$PY0116" "$B/probe_456.py" "$WK/456-0116" "$OUT/probe_456_0116.json"
    run probe_456_intents_create_0116 "$PY0114" "$B/probe_456_intents.py" create "$WK/int-0116" "$OUT/probe_456_intents_create_0116.json"
    run probe_456_intents_0116 "$PY0116" "$B/probe_456_intents.py" upgrade "$WK/int-0116" "$OUT/probe_456_intents_0116.json" "$GUIDE" /bin/bash
    run probe_456_intents_create_0114 "$PY0114" "$B/probe_456_intents.py" create "$WK/int-0114" "$OUT/probe_456_intents_create_0114.json"
    run probe_456_intents_0114 "$PY0114" "$B/probe_456_intents.py" upgrade "$WK/int-0114" "$OUT/probe_456_intents_0114.json" "$GUIDE" /bin/bash
  fi
fi
if [[ -f "$B/probe_456_guide.py" ]]; then
  run probe_456_guide "$PY" "$B/probe_456_guide.py" "$GUIDE" "$BIN14" "$MIXED16" "$BIN" /bin/bash "$WK/guide" "$OUT/probe_456_guide.json"
  if [[ "$CONTROL" == "1" ]]; then
    run probe_456_guide_0116 "$PY0116" "$B/probe_456_guide.py" "$GUIDE" "$BIN14" "$MIXED16" "$MIXED16" /bin/bash "$WK/guide-0116" "$OUT/probe_456_guide_0116.json"
  fi
fi
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
