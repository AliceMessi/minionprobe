# #364 on the runner (sourced by linux_setup.sh from an empty directory; needs
# NEED_0114=1). The real room CLI on PostgreSQL 16 and, for parity, on SQLite,
# on the installed 0.1.15; then on the published 0.1.14: PostgreSQL as the negative
# control, SQLite as the backend parity control.
set +e
run364() {  # run364 <name> <python> <probe> args...
  local name=$1 py=$2 probe=$3; shift 3
  echo "=== $name"
  "$py" -I -X utf8 "$B/$probe" "$RUNNER_TEMP/work/$name" "$OUT/$name.json" "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^SUMMARY" "$OUT/$name.log"
}
run364 probe_364_postgres "$PY" probe_364.py --backend postgres
run364 probe_364_sqlite "$PY" probe_364.py --backend sqlite
run364 probe_364_postgres_0114 "$PY0114" probe_364.py --backend postgres
run364 probe_364_sqlite_0114 "$PY0114" probe_364.py --backend sqlite
