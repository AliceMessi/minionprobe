#!/bin/bash
# #452 on native Windows (Git Bash). Every probe runs with the 0.1.14 interpreter, from an
# empty directory outside any checkout, with `python -I -B -X utf8`.
# Required variables:
#   W  work root: venv (openminion==0.1.14 from PyPI), venv0113 (0.1.13), venv0112 (0.1.12),
#      and the empty directory empty-452
#   E  the evidence folder openminion-0.1.14-verification
# probe_452_boundary.py runs the older package's own turns with venv0113 or venv0112; its
# outputs go to windows/452/older-<version>/.
set -u
: "${W:?}" "${E:?}"
O="$E/windows/452"
mkdir -p "$O/older-0.1.13" "$O/older-0.1.12"
cd "$W/empty-452" || exit 1
export VERIFY_WORK="$(cygpath -w "$W")"
(cd "$E/probes" && sha256sum _common.py probe_452_lib.py probe_452.py probe_452_cases.py probe_452_boundary.py \
  probe_452_published.py run_452_windows.sh steps_452.sh) > "$O/probe-sha256.txt"
uv pip list --python "$W/venv/Scripts/python.exe" > "$O/pip-list.txt" 2>/dev/null
uv pip show --python "$W/venv/Scripts/python.exe" openminion > "$O/pip-show.txt" 2>/dev/null
uv pip show --python "$W/venv0113/Scripts/python.exe" openminion > "$O/older-0.1.13/pip-show-0.1.13.txt" 2>/dev/null
uv pip show --python "$W/venv0112/Scripts/python.exe" openminion > "$O/older-0.1.12/pip-show-0.1.12.txt" 2>/dev/null
{ cmd //c ver | tr -d '\r' | sed '/^$/d'; "$W/venv/Scripts/python.exe" -I -c 'import sys,sqlite3,platform;print("python", sys.version.split()[0], "sqlite", sqlite3.sqlite_version, platform.platform())'; echo "sqlite3 shell $(sqlite3 --version | cut -c1-40)"; echo "sha256sum $(sha256sum --version | head -1)"; } > "$O/os.txt"
PY="$W/venv/Scripts/python.exe"
run() {  # run <out-dir> <name> <args...>
  local dir=$1 name=$2; shift 2
  "$PY" -I -B -X utf8 "$@" > "$dir/$name.log" 2>&1
  echo "$name exit=$?" | tee -a "$dir/$name.log"
  grep -h "^SUMMARY" "$dir/$name.log"
}
w() { cygpath -w "$1"; }
run "$O" probe_452_published "$(w "$E/probes/probe_452_published.py")" "$(w "$W/work/452-published")" "$(w "$O/probe_452_published.json")"
run "$O" probe_452 "$(w "$E/probes/probe_452.py")" "$(w "$W/work/452")" "$(w "$O/probe_452.json")"
run "$O" probe_452_cases "$(w "$E/probes/probe_452_cases.py")" "$(w "$W/work/452-cases")" "$(w "$O/probe_452_cases.json")"
run "$O/older-0.1.13" probe_452_boundary "$(w "$E/probes/probe_452_boundary.py")" "$(w "$W/venv0113/Scripts/python.exe")" \
  "$(w "$W/work/452-boundary-0113")" "$(w "$O/older-0.1.13/probe_452_boundary.json")"
run "$O/older-0.1.12" probe_452_boundary "$(w "$E/probes/probe_452_boundary.py")" "$(w "$W/venv0112/Scripts/python.exe")" \
  "$(w "$W/work/452-boundary-0112")" "$(w "$O/older-0.1.12/probe_452_boundary.json")"
