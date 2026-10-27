# #412 on the runner: probe_412 on Linux with the installed 0.1.13 and, with
# NEED_0112=1, the same probe on the published 0.1.12 (negative control).
# Sourced by linux_setup.sh from an empty directory; no PostgreSQL needed.
set +e
for v in 0113 0112; do
  if [[ "$v" == "0113" ]]; then py="$PY"; sfx=""; else py="${PY0112:-}"; sfx="_0112"; fi
  [[ -z "$py" ]] && continue
  "$py" -I -X utf8 "$B/probe_412.py" "$RUNNER_TEMP/work/412$sfx" "$OUT/probe_412$sfx.json" > "$OUT/probe_412$sfx.log" 2>&1
  echo "$v probe_412 exit=$? | $(grep SUMMARY "$OUT/probe_412$sfx.log")"
  grep -E "^FAIL " "$OUT/probe_412$sfx.log" | cut -c1-220
done
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
