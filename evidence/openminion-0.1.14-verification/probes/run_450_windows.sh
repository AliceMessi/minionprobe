#!/usr/bin/env bash
# Native Windows runs of probe_450 on the installed openminion==0.1.14, and the
# same probe on the published 0.1.13 as the negative control.
#
#   bash run_450_windows.sh <work_dir> <evidence_dir>
#
# <work_dir> holds venv (openminion==0.1.14 from PyPI) and venv0113
# (openminion==0.1.13 from PyPI), both CPython 3.12.11. Each probe runs with
# `python -I -X utf8` from an empty directory outside any checkout. The 0.1.13
# control runs first; the 0.1.14 run takes its JSON as the baseline, to compare
# the module's output with 0.1.13's case by case.
set -u
W="$1"; E="$2"; P="$E/probes"
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH \
      OPENMINION_STORAGE_POSTGRES_URL OPENMINION_TEST_POSTGRES_URL
O="$E/windows/450"; mkdir -p "$O/control-0.1.13" "$W/empty-450"
(cd "$P" && sha256sum _common.py probe_450.py run_450_windows.sh) > "$O/probe-sha256.txt"
cd "$W/empty-450" || exit 1
"$W/venv0113/Scripts/python.exe" -I -X utf8 "$P/probe_450.py" "$(cygpath -w "$W/work/450-0113")" \
  "$(cygpath -w "$O/control-0.1.13/probe_450.json")" > "$O/control-0.1.13/probe_450.log" 2>&1
echo "exit=$?" >> "$O/control-0.1.13/probe_450.log"
echo "0.1.13 probe_450 $(tail -1 "$O/control-0.1.13/probe_450.log") | $(grep SUMMARY "$O/control-0.1.13/probe_450.log")"
"$W/venv/Scripts/python.exe" -I -X utf8 "$P/probe_450.py" "$(cygpath -w "$W/work/450")" \
  "$(cygpath -w "$O/probe_450.json")" "$(cygpath -w "$O/control-0.1.13/probe_450.json")" > "$O/probe_450.log" 2>&1
echo "exit=$?" >> "$O/probe_450.log"
echo "0.1.14 probe_450 $(tail -1 "$O/probe_450.log") | $(grep SUMMARY "$O/probe_450.log")"
echo "empty dir after: $(ls -A "$W/empty-450" | wc -l) entries"
