# #390 on the runner (sourced by linux_setup.sh with NEED_0113=1, from an empty
# directory; OPENMINION_TEST_POSTGRES_URL points at postgres:16).
#   probe_390 --postgres: the SQLite/built-in store cases, the success and
#   external controls, and the PostgreSQL cases (engine disposal,
#   pg_stat_activity), with the installed 0.1.14 and, as the negative
#   control, the published 0.1.13.
set +e
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
WK="$RUNNER_TEMP/work"
run() {  # run <name> <python> <args...>
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL |^SUMMARY" "$OUT/$name.log" | cut -c1-220
}
run probe_390 "$PY" "$B/probe_390.py" "$WK/390" "$OUT/probe_390.json" --postgres
run probe_390_0113 "$PY0113" "$B/probe_390.py" "$WK/390-0113" "$OUT/probe_390_0113.json" --postgres
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
