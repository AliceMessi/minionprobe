#!/usr/bin/env bash
# Native Windows run of probe_449: first the published 0.1.14 (negative
# control), then the installed openminion==0.1.15 with the control's JSON as
# the baseline for the cases whose behavior #468 must keep.
#
#   bash run_449_windows.sh <work_dir> <probes_dir> <out_dir>
#
# <work_dir> holds venv (openminion==0.1.15 from PyPI) and venv0114
# (openminion==0.1.14 from PyPI), both CPython 3.12.11. Outputs:
# <out_dir>/probe_449.{json,log}, <out_dir>/control-0.1.14/probe_449.{json,log}
# and <out_dir>/probe-sha256.txt. The probe runs with `python -I -X utf8` from
# an empty directory outside any checkout; no PostgreSQL on Windows (cases PR
# and PF run on the Linux runner).
set -u
W="$1"; P="$2"; O="$3"
if [[ ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_TEST_POSTGRES_URL
mkdir -p "$O/control-0.1.14" "$W/empty-449"
rm -rf "$W/work/449" "$W/work/449-0114"
(cd "$P" && sha256sum _common.py probe_449.py probe_449_lib.py probe_449_ext.py run_449_windows.sh steps_449.sh) > "$O/probe-sha256.txt"
cd "$W/empty-449" || exit 1
"$W/venv0114/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_449.py")" "$(cygpath -w "$W/work/449-0114")" "$(cygpath -w "$O/control-0.1.14/probe_449.json")" - 14 3 > "$O/control-0.1.14/probe_449.log" 2>&1
echo "0.1.14 probe_449 exit=$? | $(grep SUMMARY "$O/control-0.1.14/probe_449.log")"
"$W/venv/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_449.py")" "$(cygpath -w "$W/work/449")" "$(cygpath -w "$O/probe_449.json")" "$(cygpath -w "$O/control-0.1.14/probe_449.json")" 14 3 > "$O/probe_449.log" 2>&1
echo "0.1.15 probe_449 exit=$? | $(grep SUMMARY "$O/probe_449.log")"
echo "empty dir after: $(ls -A "$W/empty-449" | wc -l) entries"
