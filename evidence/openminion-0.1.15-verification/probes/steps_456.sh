# #456 on the runner (sourced by linux_setup.sh with NEED_0114=1, from an empty
# directory).
#   probe_456: new jobs (home K: built-in store, forged metadata, ordinary and
#   forged cron jobs; home I: reference-sqlite, then reopened), with the
#   published 0.1.14 (negative control) and the installed 0.1.15.
#   probe_456_upgrade: a job created by 0.1.14 ("create", run with $PY0114),
#   then the same runtime homes opened by 0.1.15 ("upgrade"); and the control:
#   the same "create", then "upgrade" with 0.1.14 again.
set +e
WK="$RUNNER_TEMP/work"
run() {  # run <name> <python> <args...>
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL |^SUMMARY" "$OUT/$name.log" | cut -c1-220
}
run probe_456_0114 "$PY0114" "$B/probe_456.py" "$WK/456-0114" "$OUT/probe_456_0114.json"
run probe_456 "$PY" "$B/probe_456.py" "$WK/456" "$OUT/probe_456.json"
run probe_456_upgrade_create "$PY0114" "$B/probe_456_upgrade.py" create "$WK/456up" "$OUT/probe_456_upgrade_create.json"
run probe_456_upgrade "$PY" "$B/probe_456_upgrade.py" upgrade "$WK/456up" "$OUT/probe_456_upgrade.json"
run probe_456_upgrade_create_0114 "$PY0114" "$B/probe_456_upgrade.py" create "$WK/456up-0114" "$OUT/probe_456_upgrade_create_0114.json"
run probe_456_upgrade_0114 "$PY0114" "$B/probe_456_upgrade.py" upgrade "$WK/456up-0114" "$OUT/probe_456_upgrade_0114.json"
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
