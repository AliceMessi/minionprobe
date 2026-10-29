# #437 on the runner (sourced by linux_setup.sh from an empty directory; needs
# NEED_0114=1). probe_437.py on the installed 0.1.15 (SQLite controls and the
# PostgreSQL 16 cases), then the same probe on the published 0.1.14 as the
# negative control.
set +e
run437() {  # run437 <name> <python> <probe> args...
  local name=$1 py=$2 probe=$3; shift 3
  echo "=== $name"
  "$py" -I -X utf8 "$B/$probe" "$RUNNER_TEMP/work/$name" "$OUT/$name.json" "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^SUMMARY" "$OUT/$name.log"
}
"$PY" -I -c "import sqlalchemy, psycopg; print('sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__)" > "$OUT/versions.txt" 2>&1
"$PY0114" -I -c "import sqlalchemy, psycopg; print('0.1.14 venv: sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__)" >> "$OUT/versions.txt" 2>&1
run437 probe_437 "$PY" probe_437.py
run437 probe_437_0114 "$PY0114" probe_437.py
