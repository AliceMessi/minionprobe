# probe_rollback_companions.py (round 3) on the runner (sourced by linux_setup.sh from
# an empty directory), for 0.1.13 from PyPI ($PY) and the PR head 2d8fd446 ($PYHEAD).
set +e
WK="$RUNNER_TEMP/work"
run() {  # run <name> <python> <args...>
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -B -X utf8 "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^(PASS|FAIL) |^SUMMARY" "$OUT/$name.log" | cut -c1-200
}
for pair in "0.1.13:$PY" "head-2d8fd446:$PYHEAD"; do
  label=${pair%%:*}; py=${pair#*:}
  run "probe_rollback_companions_$label" "$py" "$B/probe_rollback_companions.py" "$WK/$label/companions" "$OUT/probe_rollback_companions_$label.json"
done
