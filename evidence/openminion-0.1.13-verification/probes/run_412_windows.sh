#!/usr/bin/env bash
# Native Windows run of probe_412 on the installed openminion==0.1.13 and the
# same probe on the published 0.1.12 (negative control).
#
#   bash run_412_windows.sh <work_dir> <probes_dir> <out_dir> [venv]
#
# <work_dir> holds venv (openminion==0.1.13 from PyPI; the default) and
# venv0112 (openminion==0.1.12 from PyPI), both CPython 3.12.11. Outputs:
# <out_dir>/probe_412.{json,log}, <out_dir>/control-0.1.12/probe_412.{json,log}
# and <out_dir>/probe-sha256.txt. The probe runs with `python -I -X utf8` from
# an empty directory outside any checkout. WORKDIR overrides the scratch root
# for the probe's databases (default <work_dir>/work).
set -u
W="$1"; P="$2"; O="$3"; V="${4:-venv}"
WORKDIR="${WORKDIR:-$W/work}"
if [[ "$V" == "venv" && ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist (0.1.13 not on PyPI yet)"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH
mkdir -p "$O/control-0.1.12" "$W/empty-412"
(cd "$P" && sha256sum _common.py probe_412.py probe_412_lib.py probe_412_child.py run_412_windows.sh) > "$O/probe-sha256.txt"
cd "$W/empty-412" || exit 1
"$W/$V/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_412.py")" "$(cygpath -w "$WORKDIR/412")" "$(cygpath -w "$O/probe_412.json")" > "$O/probe_412.log" 2>&1
echo "$V probe_412 exit=$? | $(grep SUMMARY "$O/probe_412.log")"
"$W/venv0112/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_412.py")" "$(cygpath -w "$WORKDIR/412-0112")" "$(cygpath -w "$O/control-0.1.12/probe_412.json")" > "$O/control-0.1.12/probe_412.log" 2>&1
echo "0.1.12 probe_412 exit=$? | $(grep SUMMARY "$O/control-0.1.12/probe_412.log")"
echo "empty dir after: $(ls -A "$W/empty-412" | wc -l) entries"
