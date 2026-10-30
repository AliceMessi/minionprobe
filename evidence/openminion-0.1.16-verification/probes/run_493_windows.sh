#!/usr/bin/env bash
# Native Windows run of probe_493 (#493, SQLite): first the installed
# openminion==0.1.16, then the published 0.1.15 as the control.
#
#   bash run_493_windows.sh <work_dir> <probes_dir> <out_dir>
#
# <work_dir> holds venv (openminion==0.1.16 from PyPI) and venv0115
# (openminion==0.1.15 from PyPI), both CPython 3.12.11. Outputs:
# <out_dir>/probe_493.{json,log}, <out_dir>/control-0.1.15/probe_493.{json,log}
# and <out_dir>/probe-sha256.txt. The probe runs with `python -I -X utf8` from
# an empty directory outside any checkout; no PostgreSQL on Windows (it runs on
# the Linux runner, steps_493.sh).
set -u
W="$1"; P="$2"; O="$3"
if [[ ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH \
      OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL OPENMINION_TEST_POSTGRES_URL PGURL
mkdir -p "$O/control-0.1.15" "$W/empty-493"
rm -rf "$W/work/493" "$W/work/493-0115"
(cd "$P" && sha256sum _common.py probe_493.py run_493_windows.sh steps_493.sh) > "$O/probe-sha256.txt"
cd "$W/empty-493" || exit 1
"$W/venv/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_493.py")" "$(cygpath -w "$W/work/493")" "$(cygpath -w "$O/probe_493.json")" --backend sqlite > "$O/probe_493.log" 2>&1
echo "exit=$?" >> "$O/probe_493.log"
echo "0.1.16 probe_493 $(tail -1 "$O/probe_493.log") | $(grep SUMMARY "$O/probe_493.log")"
"$W/venv0115/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_493.py")" "$(cygpath -w "$W/work/493-0115")" "$(cygpath -w "$O/control-0.1.15/probe_493.json")" --backend sqlite > "$O/control-0.1.15/probe_493.log" 2>&1
echo "exit=$?" >> "$O/control-0.1.15/probe_493.log"
echo "0.1.15 probe_493 $(tail -1 "$O/control-0.1.15/probe_493.log") | $(grep SUMMARY "$O/control-0.1.15/probe_493.log")"
echo "empty dir after: $(ls -A "$W/empty-493" | wc -l) entries"
