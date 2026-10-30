# #493 on the runner (sourced by linux_setup.sh from an empty directory; needs
# NEED_0115=1). probe_493.py through the real `openminion storage migrate`
# console script: on new SQLite files, and on new PostgreSQL 16 databases (the
# `public` schema of one, a named schema of another), each holding only the
# module under test; first on the installed 0.1.16, then on the published
# 0.1.15 as the control.
set +e
run493() {  # run493 <name> <python> args...
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$B/probe_493.py" "$RUNNER_TEMP/work/$name" "$OUT/$name.json" "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL |^SUMMARY|^NOT RUN" "$OUT/$name.log" | cut -c1-240
}
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
"$PY" -I -c "import sqlalchemy, psycopg, alembic; print('sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'alembic', alembic.__version__)" > "$OUT/versions.txt" 2>&1
"$PY0115" -I -c "import sqlalchemy, psycopg, alembic; print('0.1.15 venv: sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'alembic', alembic.__version__)" >> "$OUT/versions.txt" 2>&1
run493 probe_493_sqlite "$PY" --backend sqlite
run493 probe_493_postgres "$PY" --backend postgres
run493 probe_493_sqlite_0115 "$PY0115" --backend sqlite
run493 probe_493_postgres_0115 "$PY0115" --backend postgres
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
