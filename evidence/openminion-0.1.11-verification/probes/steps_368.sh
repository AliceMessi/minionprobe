# #361 and #368 on the runner: the PostgreSQL cases (#368) plus SQLite/mock/remote and
# the #361 SDK and cleanup probes for Linux parity. Sourced by linux_setup.sh from an
# empty directory; OPENMINION_TEST_POSTGRES_URL points at the postgres:16 container.
set +e
for p in probe_361 probe_361_cleanup probe_368 probe_368_pg; do
  echo "=== $p"
  "$PY" -I "$B/$p.py" "$RUNNER_TEMP/work/$p" "$OUT/$p.json" > "$OUT/$p.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$p.log"
  grep -E "^(PASS|FAIL) |checks pass" "$OUT/$p.log"
done
