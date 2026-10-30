# #448 on the runner (sourced by linux_setup.sh from an empty directory; needs
# NEED_0115=1). probe_448.py on the installed 0.1.16, first with SQLite runtime
# storage and the built-in SQLite memory store, then with PostgreSQL 16 runtime
# storage and the built-in PostgreSQL memory store (a new database per case);
# then the same two runs on the published 0.1.15 as the control.
set +e
run448() {  # run448 <name> <python> args...
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$B/probe_448.py" "$RUNNER_TEMP/work/$name" "$OUT/$name.json" "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL |^SUMMARY|case error" "$OUT/$name.log" | cut -c1-240
}
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
"$PY" -I -c "import sqlalchemy, psycopg, sophiagraph, httpx; print('sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'httpx', httpx.__version__)" > "$OUT/versions.txt" 2>&1
"$PY0115" -I -c "import sqlalchemy, psycopg, httpx; print('0.1.15 venv: sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'httpx', httpx.__version__)" >> "$OUT/versions.txt" 2>&1
run448 probe_448_sqlite "$PY" --backend sqlite
run448 probe_448_postgres "$PY" --backend postgres
run448 probe_448_sqlite_0115 "$PY0115" --backend sqlite
run448 probe_448_postgres_0115 "$PY0115" --backend postgres
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
