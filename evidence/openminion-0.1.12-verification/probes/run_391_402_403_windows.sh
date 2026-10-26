#!/usr/bin/env bash
# Native Windows runs of probe_391, probe_402 and probe_403 on the installed
# openminion==0.1.12 (and the same probes on 0.1.11 as the negative control).
#
#   bash run_391_402_403_windows.sh <work_dir> <evidence_dir>
#
# <work_dir> holds venv (openminion==0.1.12 from PyPI) and venv0111
# (openminion==0.1.11 from PyPI), both CPython 3.12.11. Each probe runs with
# `python -I -X utf8` from an empty directory outside any checkout.
set -u
W="$1"; E="$2"; P="$E/probes"
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH MEMCTL_CONFIG
for n in 391 402 403; do
  O="$E/windows/$n"; mkdir -p "$O/control-0.1.11" "$W/empty-$n"
  (cd "$P" && sha256sum _common.py "probe_$n.py" run_391_402_403_windows.sh) > "$O/probe-sha256.txt"
  cd "$W/empty-$n" || exit 1
  "$W/venv/Scripts/python.exe" -I -X utf8 "$P/probe_$n.py" "$(cygpath -w "$W/work/$n")" "$(cygpath -w "$O/probe_$n.json")" > "$O/probe_$n.log" 2>&1
  echo "0.1.12 probe_$n exit=$? | $(grep SUMMARY "$O/probe_$n.log")"
  "$W/venv0111/Scripts/python.exe" -I -X utf8 "$P/probe_$n.py" "$(cygpath -w "$W/work/$n-0111")" "$(cygpath -w "$O/control-0.1.11/probe_$n.json")" > "$O/control-0.1.11/probe_$n.log" 2>&1
  echo "0.1.11 probe_$n exit=$? | $(grep SUMMARY "$O/control-0.1.11/probe_$n.log")"
  echo "empty dir after: $(ls -A "$W/empty-$n" | wc -l) entries"
done
