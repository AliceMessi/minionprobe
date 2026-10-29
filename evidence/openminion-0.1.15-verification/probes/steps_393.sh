# #393 on the runner (sourced by linux_setup.sh from an empty directory; needs
# NEED_0114=1). probe_393.py on one SQLite file and on one PostgreSQL 16 schema,
# first with the published 0.1.14 (negative control), then with the installed
# 0.1.15; 200 rooms per unpaused stress case.
set +e
run393() {  # run393 <name> <python> args...
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$B/probe_393.py" "$RUNNER_TEMP/work/$name" "$OUT/$name.json" "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^SUMMARY" "$OUT/$name.log"
}
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
"$PY" -I -c "import sqlalchemy, psycopg; print('sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__)" > "$OUT/versions.txt" 2>&1
"$PY0114" -I -c "import sqlalchemy, psycopg; print('0.1.14 venv: sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__)" >> "$OUT/versions.txt" 2>&1
run393 probe_393_sqlite_0114 "$PY0114" --backend sqlite --rooms 200
run393 probe_393_postgres_0114 "$PY0114" --backend postgres --rooms 200
run393 probe_393_sqlite "$PY" --backend sqlite --rooms 200
run393 probe_393_postgres "$PY" --backend postgres --rooms 200
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
