# Both probes on the runner (sourced by linux_setup.sh from an empty directory),
# for 0.1.13 from PyPI ($PY) and dev @ 68d623d1 ($PYDEV); $PY0112 seeds the old
# working state.
set +e
WK="$RUNNER_TEMP/work"
run() {  # run <name> <python> <args...>
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -B -X utf8 "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^(PASS|FAIL) |^SUMMARY" "$OUT/$name.log" | cut -c1-200
}
run probe_backup_scope_0.1.13 "$PY" "$B/probe_backup_scope.py" "$WK/0.1.13/backup" "$OUT/probe_backup_scope_0.1.13.json"
run probe_typed_save_boundary_0.1.13 "$PY" "$B/probe_typed_save_boundary.py" "$PY0112" "$WK/0.1.13/typed" "$OUT/probe_typed_save_boundary_0.1.13.json"
run probe_backup_scope_dev-68d623d1 "$PYDEV" "$B/probe_backup_scope.py" "$WK/dev/backup" "$OUT/probe_backup_scope_dev-68d623d1.json"
run probe_typed_save_boundary_dev-68d623d1 "$PYDEV" "$B/probe_typed_save_boundary.py" "$PY0112" "$WK/dev/typed" "$OUT/probe_typed_save_boundary_dev-68d623d1.json"
