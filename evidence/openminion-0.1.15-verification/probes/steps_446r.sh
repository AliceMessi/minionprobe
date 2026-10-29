# #446 reporting part on the runner (sourced by linux_setup.sh from an empty
# directory; needs NEED_0114=1). probe_446r.py through the real
# `openminion storage migrate` console script, on SQLite and on new PostgreSQL
# 16 databases: first the published 0.1.14 (negative control), then the
# installed 0.1.15 with the control's JSON as the baseline.
set +e
run446() {  # run446 <name> <python> args...
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$B/probe_446r.py" "$RUNNER_TEMP/work/$name" "$OUT/$name.json" "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^SUMMARY" "$OUT/$name.log"
}
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
"$PY" -I -c "import sqlalchemy, psycopg, alembic; print('sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'alembic', alembic.__version__)" > "$OUT/versions.txt" 2>&1
"$PY0114" -I -c "import sqlalchemy, psycopg, alembic; print('0.1.14 venv: sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'alembic', alembic.__version__)" >> "$OUT/versions.txt" 2>&1
run446 probe_446r_sqlite_0114 "$PY0114" --backend sqlite
run446 probe_446r_sqlite "$PY" --backend sqlite --baseline "$OUT/probe_446r_sqlite_0114.json"
run446 probe_446r_postgres_0114 "$PY0114" --backend postgres
run446 probe_446r_postgres "$PY" --backend postgres --baseline "$OUT/probe_446r_postgres_0114.json"
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
