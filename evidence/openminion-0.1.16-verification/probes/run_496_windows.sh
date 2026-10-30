#!/usr/bin/env bash
# Native Windows run of probe_496 (built-in SQLite memory store): first the
# published 0.1.15 (the control), then the installed openminion==0.1.16 with the
# control's JSON as the baseline for the unchanged behavior.
#
#   bash run_496_windows.sh <work_dir> <probes_dir> <out_dir>
#
# <work_dir> holds venv (openminion==0.1.16 from PyPI) and venv0115
# (openminion==0.1.15 from PyPI), both CPython 3.12.11. Outputs:
# <out_dir>/probe_496.{json,log}, <out_dir>/control-0.1.15/probe_496.{json,log}
# and <out_dir>/probe-sha256.txt. The probe runs with `python -I -X utf8` from
# an empty directory outside any checkout; no PostgreSQL on Windows (the
# PostgreSQL cases run on the Linux runner, steps_496.sh).
set -u
W="$1"; P="$2"; O="$3"
if [[ ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_TEST_POSTGRES_URL OPENMINION_DISABLE_SECURITY_POLICY
mkdir -p "$O/control-0.1.15" "$W/empty-496"
rm -rf "$W/work/496" "$W/work/496-0115"
(cd "$P" && sha256sum _common.py probe_420_lib.py probe_496.py probe_496_lib.py run_496_windows.sh steps_496.sh) > "$O/probe-sha256.txt"
cd "$W/empty-496" || exit 1
"$W/venv0115/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_496.py")" "$(cygpath -w "$W/work/496-0115")" "$(cygpath -w "$O/control-0.1.15/probe_496.json")" --backend sqlite --expect-version 0.1.15 > "$O/control-0.1.15/probe_496.log" 2>&1
echo "exit=$?" >> "$O/control-0.1.15/probe_496.log"
echo "0.1.15 probe_496 $(tail -1 "$O/control-0.1.15/probe_496.log") | $(grep SUMMARY "$O/control-0.1.15/probe_496.log")"
"$W/venv/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_496.py")" "$(cygpath -w "$W/work/496")" "$(cygpath -w "$O/probe_496.json")" --backend sqlite --baseline "$(cygpath -w "$O/control-0.1.15/probe_496.json")" > "$O/probe_496.log" 2>&1
echo "exit=$?" >> "$O/probe_496.log"
echo "0.1.16 probe_496 $(tail -1 "$O/probe_496.log") | $(grep SUMMARY "$O/probe_496.log")"
echo "empty dir after: $(ls -A "$W/empty-496" | wc -l) entries"
