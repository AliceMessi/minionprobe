# #413 and #401 on the runner (sourced by linux_setup.sh with NEED_0112=1, from
# an empty directory; OPENMINION_TEST_POSTGRES_URL points at postgres:16).
#   probe_413: memctl inspect with omitted/empty/blank/valid/invalid scopes and
#              the command lifecycle (SQLite; Linux parity with Windows)
#   probe_401: memctl gc on PostgreSQL through memctl's own audited store, bare
#              and audited controls, error behavior, and the SQLite controls
# Each probe also runs with the published 0.1.12 as the negative control.
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
run probe_413 "$PY" "$B/probe_413.py" "$WK/413" "$OUT/probe_413.json"
run probe_413_0112 "$PY0112" "$B/probe_413.py" "$WK/413-0112" "$OUT/probe_413_0112.json"
run probe_401 "$PY" "$B/probe_401.py" "$WK/401" "$OUT/probe_401.json" --postgres
run probe_401_0112 "$PY0112" "$B/probe_401.py" "$WK/401-0112" "$OUT/probe_401_0112.json" --postgres
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
