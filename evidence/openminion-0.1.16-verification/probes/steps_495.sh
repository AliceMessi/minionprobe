# #495 on the runner (sourced by linux_setup.sh from an empty directory; needs
# NEED_0115=1). probe_495.py on one SQLite file and on one PostgreSQL 16
# schema, first with the published 0.1.15 (the control), then with the
# installed 0.1.16; 300 rooms in the unpaused stress.
set +e
run495() {  # run495 <name> <python> args...
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$B/probe_495.py" "$RUNNER_TEMP/work/$name" "$OUT/$name.json" "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL " "$OUT/$name.log" | cut -c1-200
  grep -E "^SUMMARY" "$OUT/$name.log"
}
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
"$PY" -I -c "import sqlalchemy, psycopg; print('sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__)" > "$OUT/versions.txt" 2>&1
"$PY0115" -I -c "import sqlalchemy, psycopg; print('0.1.15 venv: sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__)" >> "$OUT/versions.txt" 2>&1
run495 probe_495_sqlite_0115 "$PY0115" --backend sqlite --rooms 300
run495 probe_495_postgres_0115 "$PY0115" --backend postgres --rooms 300
run495 probe_495_sqlite "$PY" --backend sqlite --rooms 300
run495 probe_495_postgres "$PY" --backend postgres --rooms 300
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
