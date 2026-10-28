# #450 on the runner (sourced by linux_setup.sh with NEED_0113=1, from an empty
# directory). probe_450 runs `python -m openminion.modules.storage`, the
# existing `python -m openminion.modules.storage.cli` entry point and a direct
# main() call as real subprocesses. The published 0.1.13 runs first as the
# negative control; the 0.1.14 run takes its JSON as the baseline. No
# PostgreSQL is needed for the checklist; with OPENMINION_TEST_POSTGRES_URL set
# the probe adds a `pool-stats --postgres-url` success control (URL redacted).
set +e
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
unset OPENMINION_STORAGE_POSTGRES_URL
WK="$RUNNER_TEMP/work"
run() {  # run <name> <python> <args...>
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL |^SUMMARY|^\[" "$OUT/$name.log" | cut -c1-220
}
run probe_450_0113 "$PY0113" "$B/probe_450.py" "$WK/450-0113" "$OUT/probe_450_0113.json"
run probe_450 "$PY" "$B/probe_450.py" "$WK/450" "$OUT/probe_450.json" "$OUT/probe_450_0113.json"
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
