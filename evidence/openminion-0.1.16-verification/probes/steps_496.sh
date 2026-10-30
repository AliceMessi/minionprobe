# #496 on the runner (sourced by linux_setup.sh from an empty directory, with
# NEED_0115=1). probe_496.py on the built-in SQLite memory store (cases W, D, WA,
# N, X, P, R) and on a PostgreSQL 16 memory store in its own schema per case
# (cases W, N, R): first with the published 0.1.15 (the control), then with the
# installed 0.1.16, which compares its unchanged behavior with the control's JSON.
set +e
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
WK="$RUNNER_TEMP/work"
run() {  # run <name> <python> <args...>
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL |^SUMMARY|^case " "$OUT/$name.log" | cut -c1-240
}
"$PY" -I -c "import sqlalchemy, psycopg; print('sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__)" > "$OUT/versions.txt" 2>&1
"$PY0115" -I -c "import sqlalchemy, psycopg; print('0.1.15 venv: sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__)" >> "$OUT/versions.txt" 2>&1
run probe_496_sqlite_0115 "$PY0115" "$B/probe_496.py" "$WK/496-sqlite-0115" "$OUT/probe_496_sqlite_0115.json" --backend sqlite --expect-version 0.1.15
run probe_496_postgres_0115 "$PY0115" "$B/probe_496.py" "$WK/496-postgres-0115" "$OUT/probe_496_postgres_0115.json" --backend postgres --expect-version 0.1.15
run probe_496_sqlite "$PY" "$B/probe_496.py" "$WK/496-sqlite" "$OUT/probe_496_sqlite.json" --backend sqlite --baseline "$OUT/probe_496_sqlite_0115.json"
run probe_496_postgres "$PY" "$B/probe_496.py" "$WK/496-postgres" "$OUT/probe_496_postgres.json" --backend postgres --baseline "$OUT/probe_496_postgres_0115.json"
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
