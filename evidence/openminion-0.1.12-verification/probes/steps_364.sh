# #364 on the runner (sourced by linux_setup.sh from an empty directory; needs
# NEED_0111=1). SQLite (Linux parity) and PostgreSQL 16: the fresh-store probe
# on 0.1.12 and, as the negative control, on 0.1.11; then the migration-14
# upgrade from a 0.1.11 database on both backends.
set +e
docker exec pg pg_dump --version > "$OUT/pg_dump-version.txt" 2>&1
run364() {  # run364 <name> <python> <probe> args...
  local name=$1 py=$2 probe=$3; shift 3
  echo "=== $name"
  "$py" -I -X utf8 "$B/$probe" "$RUNNER_TEMP/work/$name" "$OUT/$name.json" "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^SUMMARY" "$OUT/$name.log"
}
run364 probe_364_sqlite "$PY" probe_364.py --backend sqlite --peers 8 --joins 10 --rounds 50
run364 probe_364_postgres "$PY" probe_364.py --backend postgres --peers 12 --joins 10 --rounds 100
run364 probe_364_sqlite_0111 "$PY0111" probe_364.py --backend sqlite --peers 8 --joins 10 --rounds 50
run364 probe_364_postgres_0111 "$PY0111" probe_364.py --backend postgres --peers 12 --joins 10 --rounds 100
run364 probe_364_upgrade_sqlite "$PY" probe_364_upgrade.py --backend sqlite --rooms 400 --py0111 "$PY0111"
run364 probe_364_upgrade_postgres "$PY" probe_364_upgrade.py --backend postgres --rooms 400 --py0111 "$PY0111"
