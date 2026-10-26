# #392, #395 and #400 on the runner (sourced by linux_setup.sh with NEED_0111=1,
# from an empty directory; OPENMINION_TEST_POSTGRES_URL points at postgres:16).
#   probe_392: SDK session lifecycle + retention helpers, SQLite and PostgreSQL
#   probe_400: decoder, new evidence, 0.1.11-written history, invalid JSON, wrapper
#   probe_395: freshness contract on a continuation entry (scripted provider)
# Each probe also runs with the published 0.1.11 as the negative control.
set +e
WK="$RUNNER_TEMP/work"
run() {  # run <name> <python> <args...>
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^(PASS|FAIL) |^SUMMARY" "$OUT/$name.log" | cut -c1-220
}
run probe_392 "$PY" "$B/probe_392.py" "$WK/392" "$OUT/probe_392.json"
run probe_392_0111 "$PY0111" "$B/probe_392.py" "$WK/392-0111" "$OUT/probe_392_0111.json"
# #400: history written by the published 0.1.11, then read and collected by 0.1.12.
run probe_400_seed_by_0111 "$PY0111" "$B/probe_400.py" seed "$WK/400" "$OUT/probe_400_seed_by_0111.json"
run probe_400 "$PY" "$B/probe_400.py" run "$WK/400" "$OUT/probe_400.json"
# Control: the same with 0.1.11 on both sides.
run probe_400_0111_seed "$PY0111" "$B/probe_400.py" seed "$WK/400-0111" "$OUT/probe_400_0111_seed.json"
run probe_400_0111 "$PY0111" "$B/probe_400.py" run "$WK/400-0111" "$OUT/probe_400_0111.json"
run probe_395 "$PY" "$B/probe_395.py" "$WK/395" "$OUT/probe_395.json"
run probe_395_0111 "$PY0111" "$B/probe_395.py" "$WK/395-0111" "$OUT/probe_395_0111.json"
