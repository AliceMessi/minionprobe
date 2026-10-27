#!/usr/bin/env bash
# Native Windows runs on the installed openminion==0.1.13 (and the same probes
# on 0.1.12 as the negative control): probe_413, and probe_401's SQLite
# controls (PostgreSQL runs on the Linux runner, steps_413_401.sh).
#
#   bash run_413_401_windows.sh <work_dir> <evidence_dir>
#
# <work_dir> holds venv (openminion==0.1.13 from PyPI) and venv0112
# (openminion==0.1.12 from PyPI), both CPython 3.12.11. Each probe runs with
# `python -I -X utf8` from an empty directory outside any checkout.
set -u
W="$1"; E="$2"; P="$E/probes"
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH MEMCTL_CONFIG OPENMINION_TEST_POSTGRES_URL
for n in 413 401; do
  O="$E/windows/$n"; mkdir -p "$O/control-0.1.12" "$W/empty-$n"
  files="_common.py probe_$n.py run_413_401_windows.sh"; [[ $n == 401 ]] && files="_common.py probe_401.py probe_401_lib.py run_413_401_windows.sh"
  (cd "$P" && sha256sum $files) > "$O/probe-sha256.txt"
  cd "$W/empty-$n" || exit 1
  "$W/venv/Scripts/python.exe" -I -X utf8 "$P/probe_$n.py" "$(cygpath -w "$W/work/$n")" "$(cygpath -w "$O/probe_$n.json")" > "$O/probe_$n.log" 2>&1
  echo "0.1.13 probe_$n exit=$? | $(grep SUMMARY "$O/probe_$n.log")"
  "$W/venv0112/Scripts/python.exe" -I -X utf8 "$P/probe_$n.py" "$(cygpath -w "$W/work/$n-0112")" "$(cygpath -w "$O/control-0.1.12/probe_$n.json")" > "$O/control-0.1.12/probe_$n.log" 2>&1
  echo "0.1.12 probe_$n exit=$? | $(grep SUMMARY "$O/control-0.1.12/probe_$n.log")"
  echo "empty dir after: $(ls -A "$W/empty-$n" | wc -l) entries"
done
