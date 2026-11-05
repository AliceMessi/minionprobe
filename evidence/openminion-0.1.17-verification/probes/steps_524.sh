# #524 on the runner (sourced by linux_setup.sh from an empty directory; run with
# --need-0116 for the control). probe_524.py (steps_524_optional.sh runs probe_524_optional.py), on the
# built-in SQLite memory store and on a PostgreSQL 16 memory store in its own schema
# per case: first with the published 0.1.16 (the control, when PY0116 is set), then
# with the package under test ($PY, version $OM_VERSION), which compares the
# unchanged policy/approval history and decision records with the control's JSON.
set +e
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
WK="$RUNNER_TEMP/work"
run() {  # run <name> <python> <args...>
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL |^SUMMARY|^NOTE |^case .*error=[^N]" "$OUT/$name.log" | cut -c1-300
}
"$PY" -I -c "import sqlalchemy, psycopg; print('sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__)" > "$OUT/versions.txt" 2>&1
for probe in probe_524; do
  for be in sqlite postgres; do
    base=()
    if [[ -n "${PY0116:-}" ]]; then
      run "${probe}_${be}_0116" "$PY0116" "$B/$probe.py" "$WK/$probe-$be-0116" "$OUT/${probe}_${be}_0116.json" --backend "$be" --expect-version 0.1.16
      base=(--baseline "$OUT/${probe}_${be}_0116.json")
    fi
    run "${probe}_${be}" "$PY" "$B/$probe.py" "$WK/$probe-$be" "$OUT/${probe}_${be}.json" --backend "$be" --expect-version "$OM_VERSION" "${base[@]}"
  done
done
docker exec pg psql -U postgres -tAc "select count(*) from information_schema.schemata where schema_name like 'probe496_%'" > "$OUT/postgres-leftover-schemas.txt" 2>&1
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
