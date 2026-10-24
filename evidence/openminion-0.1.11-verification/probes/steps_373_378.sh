# #373 and #378 on Linux (parity for the connection-closure checks; the rename/delete
# parts pass trivially on Linux, where an open handle does not block unlink).
for p in probe_373 probe_378; do
  "$PY" -I -X utf8 "$B/$p.py" "$RUNNER_TEMP/work/$p" "$OUT/$p.json" > "$OUT/$p.log" 2>&1
  echo "$p rc=$? $(grep SUMMARY "$OUT/$p.log")"
done
