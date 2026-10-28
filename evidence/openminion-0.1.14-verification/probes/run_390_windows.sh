#!/usr/bin/env bash
# Native Windows run of probe_390 on the installed openminion==0.1.14 and the
# same probe on the published 0.1.13 (negative control).
#
#   bash run_390_windows.sh <work_dir> <probes_dir> <out_dir>
#
# <work_dir> holds venv (openminion==0.1.14 from PyPI) and venv0113
# (openminion==0.1.13 from PyPI), both CPython 3.12.11. Outputs:
# <out_dir>/probe_390.{json,log}, <out_dir>/control-0.1.13/probe_390.{json,log}
# and <out_dir>/probe-sha256.txt. The probe runs with `python -I -X utf8` from
# an empty directory outside any checkout; no PostgreSQL on Windows.
set -u
W="$1"; P="$2"; O="$3"
if [[ ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_TEST_POSTGRES_URL
mkdir -p "$O/control-0.1.13" "$W/empty-390"
(cd "$P" && sha256sum _common.py probe_390.py probe_390_lib.py run_390_windows.sh steps_390.sh) > "$O/probe-sha256.txt"
cd "$W/empty-390" || exit 1
"$W/venv/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_390.py")" "$(cygpath -w "$W/work/390")" "$(cygpath -w "$O/probe_390.json")" > "$O/probe_390.log" 2>&1
echo "0.1.14 probe_390 exit=$? | $(grep SUMMARY "$O/probe_390.log")"
"$W/venv0113/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_390.py")" "$(cygpath -w "$W/work/390-0113")" "$(cygpath -w "$O/control-0.1.13/probe_390.json")" > "$O/control-0.1.13/probe_390.log" 2>&1
echo "0.1.13 probe_390 exit=$? | $(grep SUMMARY "$O/control-0.1.13/probe_390.log")"
echo "empty dir after: $(ls -A "$W/empty-390" | wc -l) entries"
