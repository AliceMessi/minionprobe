# The three probes on the runner (sourced by linux_setup.sh from an empty
# directory), for 0.1.13 from PyPI ($PY) and dev @ 988ac4ec ($PYDEV); $PY0112
# seeds the old working state.
set +e
WK="$RUNNER_TEMP/work"
run() {  # run <name> <python> <args...>
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -B -X utf8 "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^(PASS|FAIL) |^SUMMARY" "$OUT/$name.log" | cut -c1-200
}
for pair in "0.1.13:$PY" "dev-988ac4ec:$PYDEV"; do
  label=${pair%%:*}; py=${pair#*:}
  run "probe_rollback_companions_$label" "$py" "$B/probe_rollback_companions.py" "$WK/$label/companions" "$OUT/probe_rollback_companions_$label.json"
  run "probe_backup_scope_$label" "$py" "$B/probe_backup_scope.py" "$WK/$label/backup" "$OUT/probe_backup_scope_$label.json"
  run "probe_typed_save_boundary_$label" "$py" "$B/probe_typed_save_boundary.py" "$PY0112" "$WK/$label/typed" "$OUT/probe_typed_save_boundary_$label.json"
done
