# #446 on the runner, second job (sourced by linux_setup.sh; needs NEED_0116=1): the installed files'
# identity (probe_446_package.py), the documented pooler limitation (probe_446_pooler.py, PgBouncer) and
# SQLite (probe_446_sqlite.py, compared with 0.1.16), each also on the published 0.1.16 as the control.
set +e
run446() {  # run446 <name> <python> <probe> args...
  local name=$1 py=$2 probe=$3; shift 3
  echo "=== $name"
  "$py" -I -X utf8 "$B/$probe" "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL |^SUMMARY|^NOT RUN" "$OUT/$name.log" | cut -c1-300
}
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
"$PY" -I -c "import sqlalchemy, psycopg, alembic; print('sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'alembic', alembic.__version__)" > "$OUT/versions.txt" 2>&1
"$PY0116" -I -c "import sqlalchemy, psycopg, alembic; print('0.1.16 venv: sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'alembic', alembic.__version__)" >> "$OUT/versions.txt" 2>&1
command -v pgbouncer >/dev/null 2>&1 || sudo apt-get install -y -qq pgbouncer > "$OUT/apt-pgbouncer.log" 2>&1
pgbouncer --version | head -1 >> "$OUT/versions.txt" 2>&1
cat "$OUT/versions.txt"
run446 probe_446_package "$PY" probe_446_package.py "$OUT/probe_446_package.json" --expect "v$OM_VERSION"
run446 probe_446_package_0116 "$PY0116" probe_446_package.py "$OUT/probe_446_package_0116.json" --expect v0.1.16
run446 probe_446_pooler "$PY" probe_446_pooler.py "$RUNNER_TEMP/work/pool" "$OUT/probe_446_pooler.json"
run446 probe_446_sqlite_0116 "$PY0116" probe_446_sqlite.py "$RUNNER_TEMP/work/sq0116" "$OUT/probe_446_sqlite_0116.json"
run446 probe_446_sqlite "$PY" probe_446_sqlite.py "$RUNNER_TEMP/work/sq" "$OUT/probe_446_sqlite.json" --baseline "$OUT/probe_446_sqlite_0116.json"
run446 probe_446_pooler_0116 "$PY0116" probe_446_pooler.py "$RUNNER_TEMP/work/pool-0116" "$OUT/probe_446_pooler_0116.json"
docker exec pg psql -U postgres -tAc "select count(*) from pg_database where datname like 'om446%'" > "$OUT/databases-left.txt" 2>&1
echo "databases left: $(cat "$OUT/databases-left.txt"); empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
