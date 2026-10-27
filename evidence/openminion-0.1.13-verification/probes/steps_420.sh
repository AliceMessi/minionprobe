# #420 on the runner (sourced by linux_setup.sh from an empty directory).
# probe_420 runs its PostgreSQL memory-store case P here (OPENMINION_TEST_POSTGRES_URL
# points at the postgres:16 container); the same probe on 0.1.12 is the negative control.
set +e
for v in 0113 0112; do
  if [[ "$v" == "0113" ]]; then py="$PY"; name="probe_420"; else py="$PY0112"; name="probe_420_0112"; fi
  echo "=== $name"
  "$py" -I -X utf8 "$B/probe_420.py" "$RUNNER_TEMP/work/$name" "$OUT/$name.json" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^(PASS|FAIL) |^SUMMARY" "$OUT/$name.log" | cut -c1-220
done
