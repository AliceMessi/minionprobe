# #388, #389 and #397 on the runner (sourced by linux_setup.sh from an empty directory).
# probe_388 and probe_389 run their PostgreSQL cases here (OPENMINION_TEST_POSTGRES_URL points
# at the postgres:16 container); everything also runs on 0.1.11 as the negative control.
set +e
for p in probe_388 probe_389 probe_397; do
  for v in 0112 0111; do
    if [[ "$v" == "0112" ]]; then py="$PY"; name="$p"; else py="$PY0111"; name="${p}_0111"; fi
    echo "=== $name"
    "$py" -I -X utf8 "$B/$p.py" "$RUNNER_TEMP/work/$name" "$OUT/$name.json" > "$OUT/$name.log" 2>&1
    echo "exit=$?" | tee -a "$OUT/$name.log"
    grep -E "^(PASS|FAIL) |^SUMMARY" "$OUT/$name.log" | cut -c1-220
  done
done
