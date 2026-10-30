#!/usr/bin/env bash
# Native Windows run of probe_495 (SQLite): first the published 0.1.15 (the
# control), then the installed openminion==0.1.16.
#
#   bash run_495_windows.sh <work_dir> <probes_dir> <out_dir>
#
# <work_dir> holds venv (openminion==0.1.16 from PyPI) and venv0115
# (openminion==0.1.15 from PyPI), both CPython 3.12.11. Outputs:
# <out_dir>/probe_495.{json,log}, <out_dir>/control-0.1.15/probe_495.{json,log}
# and <out_dir>/probe-sha256.txt. The probe runs with `python -I -X utf8` from
# an empty directory outside any checkout, 100 rooms in the unpaused stress; no
# PostgreSQL on Windows (it runs on the Linux runner, steps_495.sh).
set -u
W="$1"; P="$2"; O="$3"
if [[ ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_TEST_POSTGRES_URL
mkdir -p "$O/control-0.1.15" "$W/empty-495"
rm -rf "$W/work/495" "$W/work/495-0115"
(cd "$P" && sha256sum _common.py probe_495.py probe_495_lib.py run_495_windows.sh steps_495.sh) > "$O/probe-sha256.txt"
cd "$W/empty-495" || exit 1
"$W/venv0115/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_495.py")" "$(cygpath -w "$W/work/495-0115")" "$(cygpath -w "$O/control-0.1.15/probe_495.json")" --backend sqlite --rooms 100 > "$O/control-0.1.15/probe_495.log" 2>&1
echo "exit=$?" >> "$O/control-0.1.15/probe_495.log"
echo "0.1.15 probe_495 $(tail -1 "$O/control-0.1.15/probe_495.log") | $(grep SUMMARY "$O/control-0.1.15/probe_495.log")"
"$W/venv/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_495.py")" "$(cygpath -w "$W/work/495")" "$(cygpath -w "$O/probe_495.json")" --backend sqlite --rooms 100 > "$O/probe_495.log" 2>&1
echo "exit=$?" >> "$O/probe_495.log"
echo "0.1.16 probe_495 $(tail -1 "$O/probe_495.log") | $(grep SUMMARY "$O/probe_495.log")"
echo "empty dir after: $(ls -A "$W/empty-495" | wc -l) entries"
