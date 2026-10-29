# #449 on the runner (sourced by linux_setup.sh with NEED_0114=1, from an empty
# directory; OPENMINION_TEST_POSTGRES_URL points at postgres:16).
#   probe_449: the SQLite cases (U/R 14 + 3 runs, A, D, N1, F, N2) and the
#   PostgreSQL cases (PR: reopened with the built-in store on PostgreSQL; PF: a
#   real write failure in PostgreSQL), first with the published 0.1.14 (the
#   negative control), then with the installed 0.1.15, which compares its
#   unchanged-behavior cases against the 0.1.14 JSON.
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
run probe_449_0114 "$PY0114" "$B/probe_449.py" "$WK/449-0114" "$OUT/probe_449_0114.json" - 14 3
run probe_449 "$PY" "$B/probe_449.py" "$WK/449" "$OUT/probe_449.json" "$OUT/probe_449_0114.json" 14 3
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
