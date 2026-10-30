# #447 on the runner (sourced by linux_setup.sh with NEED_0115=1, from an empty
# directory; $PGURL points at postgres:16).
#   probe_447.py: every case on SQLite and on PostgreSQL (a new database per
#     case), first with the published 0.1.15 (the control), then with the
#     installed 0.1.16, which compares its successful-close cases with the
#     0.1.15 JSON.
#   The #447 report's own probes, unchanged (probe_447_report_retention.py and
#   probe_447_report_repro.py are byte copies), on both versions, and
#   probe_447_report_checks.py over their outputs.
set +e
WK="$RUNNER_TEMP/work"
mkdir -p "$WK"
export PGURL
run() {  # run <name> <python> <args...>
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL |^SUMMARY|^NOT RUN" "$OUT/$name.log" | cut -c1-260
}
"$PY" -I -c "import sqlalchemy, psycopg, psutil; print('sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'psutil', psutil.__version__)" > "$OUT/versions.txt" 2>&1
"$PY0115" -I -c "import sqlalchemy, psycopg, psutil; print('0.1.15 venv: sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'psutil', psutil.__version__)" >> "$OUT/versions.txt" 2>&1
run probe_447_0115 "$PY0115" "$B/probe_447.py" "$WK/447-0115" "$OUT/probe_447_0115.json" --backend all
run probe_447 "$PY" "$B/probe_447.py" "$WK/447" "$OUT/probe_447.json" --backend all --baseline "$OUT/probe_447_0115.json"
for v in 0115 0116; do
  py="$PY"; sfx=""
  if [[ $v == 0115 ]]; then py="$PY0115"; sfx="_0115"; fi
  run "probe_447_report_retention$sfx" "$py" "$B/probe_447_report_retention.py" --label "v$v" \
    --work "$WK/retention-$v" --out "$OUT/probe_447_report_retention$sfx.json"
  echo "=== probe_447_report_repro$sfx"
  "$py" -I -X utf8 "$B/probe_447_report_repro.py" "$WK/repro-$v" > "$OUT/probe_447_report_repro$sfx.txt" 2> "$OUT/probe_447_report_repro$sfx.stderr.txt"
  echo "exit=$?"; cat "$OUT/probe_447_report_repro$sfx.txt"
  run "probe_447_report_checks$sfx" "$py" "$B/probe_447_report_checks.py" "$OUT/probe_447_report_retention$sfx.json" \
    "$OUT/probe_447_report_repro$sfx.txt" "$OUT/probe_447_report_checks$sfx.json"
done
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
