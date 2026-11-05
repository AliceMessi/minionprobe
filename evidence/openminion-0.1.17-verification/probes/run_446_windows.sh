#!/usr/bin/env bash
# #446 on native Windows (Git Bash), no PostgreSQL: the installed files' identity and SQLite.
#   bash run_446_windows.sh <W> <E>
# W: the workspace with venv (openminion==0.1.17), venv0116 (==0.1.16) and empty-446; E: the evidence dir.
# Each probe runs with `python -I -X utf8` from the empty directory, one at a time; the 0.1.16 control
# first, then 0.1.17 with the control's JSON as the SQLite baseline.
set -u
W=$1; E=$2
O="$E/windows/446"; C="$O/control-0.1.16"
mkdir -p "$O" "$C" "$W/work/446"
rm -rf "$W/work/446/sqlite" "$W/work/446/sqlite-0116"
export VERIFY_WORK="$(cygpath -w "$W")"
p() { cygpath -w "$1"; }
cd "$W/empty-446" || exit 2
run() {  # run <python> <log> args...
  local py=$1 log=$2; shift 2
  "$py" -I -X utf8 "$@" > "$log" 2>&1
  echo "exit=$?" >> "$log"
  grep -E "^FAIL |^SUMMARY|^exit=" "$log" | cut -c1-300
}
run "$W/venv0116/Scripts/python.exe" "$C/probe_446_package.log" "$(p "$E/probes/probe_446_package.py")" "$(p "$C/probe_446_package.json")" --expect v0.1.16
run "$W/venv/Scripts/python.exe" "$O/probe_446_package.log" "$(p "$E/probes/probe_446_package.py")" "$(p "$O/probe_446_package.json")" --expect v0.1.17
run "$W/venv0116/Scripts/python.exe" "$C/probe_446_sqlite.log" "$(p "$E/probes/probe_446_sqlite.py")" "$(p "$W/work/446/sqlite-0116")" "$(p "$C/probe_446_sqlite.json")"
run "$W/venv/Scripts/python.exe" "$O/probe_446_sqlite.log" "$(p "$E/probes/probe_446_sqlite.py")" "$(p "$W/work/446/sqlite")" "$(p "$O/probe_446_sqlite.json")" --baseline "$(p "$C/probe_446_sqlite.json")"
echo "empty dir after: $(ls -A "$W/empty-446" | wc -l) entries"
(cd "$E/probes" && sha256sum probe_446_package.py probe_446_expected.py probe_446_sqlite.py _common.py run_446_windows.sh) > "$O/probe-sha256.txt"
