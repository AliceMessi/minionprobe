# #494 on the runner (sourced by linux_setup.sh from an empty directory; needs
# NEED_0115=1). probe_494.py on PostgreSQL 16 (items 1-3: module-owned schemas
# in new databases) and on SQLite (item 4, compared with the published 0.1.15
# through its console script), on the installed 0.1.16; then the same probe on
# the published 0.1.15 as the control.
set +e
run494() {  # run494 <name> <python> args...
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$B/probe_494.py" "$RUNNER_TEMP/work/$name" "$OUT/$name.json" "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL |^SUMMARY|^NOT RUN" "$OUT/$name.log" | cut -c1-260
}
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
"$PY" -I -c "import sqlalchemy, psycopg, alembic; print('sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'alembic', alembic.__version__)" > "$OUT/versions.txt" 2>&1
"$PY0115" -I -c "import sqlalchemy, psycopg, alembic; print('0.1.15 venv: sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'alembic', alembic.__version__)" >> "$OUT/versions.txt" 2>&1
run494 probe_494_postgres "$PY" --backend postgres
run494 probe_494_sqlite "$PY" --backend sqlite --previous-python "$PY0115"
run494 probe_494_postgres_0115 "$PY0115" --backend postgres
run494 probe_494_sqlite_0115 "$PY0115" --backend sqlite
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
