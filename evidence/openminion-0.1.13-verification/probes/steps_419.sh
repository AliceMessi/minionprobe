# #419 on the runner (sourced by linux_setup.sh with NEED_0112=1, from an empty
# directory; no PostgreSQL needed: the brain's working state is SQLite or local files).
#   probe_419: entry text freshness, same-turn evidence, controls, stored working
#              state through the SQLite and local session owners (scripted provider
#              on 127.0.0.1)
#   probe_419_upgrade: 0.1.12 writers -> backup -> 0.1.13 -> rejection on a scratch
#              copy -> rollback with the matching backup and 0.1.12
# probe_419 also runs with the published 0.1.12 as the negative control.
set +e
WK="$RUNNER_TEMP/work"
run() {  # run <name> <python> <args...>
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -B -X utf8 "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^(PASS|FAIL) |^SUMMARY" "$OUT/$name.log" | cut -c1-220
}
run probe_419 "$PY" "$B/probe_419.py" "$WK/419" "$OUT/probe_419.json"
run probe_419_upgrade "$PY" "$B/probe_419_upgrade.py" "$PY0112" "$WK/419-upgrade" "$OUT/probe_419_upgrade.json"
run probe_419_0112 "$PY0112" "$B/probe_419.py" "$WK/419-0112" "$OUT/probe_419_0112.json"
