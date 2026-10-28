# #452 on the runner (sourced by linux_setup.sh with NEED_0113=1, from an empty directory;
# no PostgreSQL needed: the guidance covers a SQLite runtime home).
#   probe_452            the documented backup and rollback on a home the installed 0.1.14 made
#   probe_452_cases      single databases with -journal/-wal/-shm, incl. the #453 review's reproduction
#   probe_452_boundary   older package -> backup -> 0.1.14 -> rollback with the older package,
#                        for 0.1.13 ($PY0113) and 0.1.12 (its own venv, made here)
# probe_452_published (GitHub/PyPI lookups) runs on Windows only.
set +e
WK="$RUNNER_TEMP/work"
command -v sqlite3 >/dev/null 2>&1 || { sudo apt-get update -qq >/dev/null 2>&1; sudo apt-get install -y -qq sqlite3 >/dev/null 2>&1; }
echo "sqlite3 shell $(sqlite3 --version | cut -c1-40)" >> "$OUT/os.txt"
uv venv -q --python 3.12 "$RUNNER_TEMP/venv0112"
uv pip install -q --python "$RUNNER_TEMP/venv0112/bin/python" --index-url https://pypi.org/simple "openminion==0.1.12"
PY0112="$RUNNER_TEMP/venv0112/bin/python"
uv pip show --python "$PY0112" openminion > "$OUT/pip-show-0.1.12.txt"
run() {  # run <name> <args...>
  local name=$1; shift
  echo "=== $name"
  "$PY" -I -B -X utf8 "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^(PASS|FAIL) |^SUMMARY" "$OUT/$name.log" | cut -c1-200
}
run probe_452 "$B/probe_452.py" "$WK/452" "$OUT/probe_452.json"
run probe_452_cases "$B/probe_452_cases.py" "$WK/452-cases" "$OUT/probe_452_cases.json"
run probe_452_boundary_0113 "$B/probe_452_boundary.py" "$PY0113" "$WK/452-boundary-0113" "$OUT/probe_452_boundary_0113.json"
run probe_452_boundary_0112 "$B/probe_452_boundary.py" "$PY0112" "$WK/452-boundary-0112" "$OUT/probe_452_boundary_0112.json"
