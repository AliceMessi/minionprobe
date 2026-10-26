# #391, #402 and #403 on the runner: Linux parity for the three probes, the
# PostgreSQL case of #402 (store.backend: postgres, postgres:16), and the same
# probes on 0.1.11 (run with NEED_0111=1). Sourced by linux_setup.sh from an
# empty directory; OPENMINION_TEST_POSTGRES_URL points at the container.
set +e
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
for v in 0112 0111; do
  if [[ "$v" == "0112" ]]; then py="$PY"; sfx=""; else py="$PY0111"; sfx="_0111"; fi
  for p in probe_391 probe_402 probe_403; do
    extra=""; [[ "$p" == "probe_402" ]] && extra="--postgres"
    "$py" -I -X utf8 "$B/$p.py" "$RUNNER_TEMP/work/$p$sfx" "$OUT/$p$sfx.json" $extra > "$OUT/$p$sfx.log" 2>&1
    echo "$v $p exit=$? | $(grep SUMMARY "$OUT/$p$sfx.log")"
    grep -E "^FAIL " "$OUT/$p$sfx.log" | cut -c1-160
  done
done
