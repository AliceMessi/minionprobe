#!/usr/bin/env bash
# Native Windows run of probe_446r (#446 reporting part, SQLite): first the
# published 0.1.14 (negative control), then the installed openminion==0.1.15
# with the control's JSON as the baseline for exit statuses and success output.
#
#   bash run_446r_windows.sh <work_dir> <probes_dir> <out_dir>
#
# <work_dir> holds venv (openminion==0.1.15 from PyPI) and venv0114
# (openminion==0.1.14 from PyPI), both CPython 3.12.11. Outputs:
# <out_dir>/probe_446r.{json,log}, <out_dir>/control-0.1.14/probe_446r.{json,log}
# and <out_dir>/probe-sha256.txt. The probe runs with `python -I -X utf8` from
# an empty directory outside any checkout; no PostgreSQL on Windows (it runs on
# the Linux runner, steps_446r.sh).
set -u
W="$1"; P="$2"; O="$3"
if [[ ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_TEST_POSTGRES_URL OPENMINION_STORAGE_POSTGRES_URL
mkdir -p "$O/control-0.1.14" "$W/empty-446"
rm -rf "$W/work/446" "$W/work/446-0114"
(cd "$P" && sha256sum _common.py probe_446r.py run_446r_windows.sh steps_446r.sh) > "$O/probe-sha256.txt"
cd "$W/empty-446" || exit 1
"$W/venv0114/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_446r.py")" "$(cygpath -w "$W/work/446-0114")" "$(cygpath -w "$O/control-0.1.14/probe_446r.json")" > "$O/control-0.1.14/probe_446r.log" 2>&1
echo "exit=$?" >> "$O/control-0.1.14/probe_446r.log"
echo "0.1.14 probe_446r $(tail -1 "$O/control-0.1.14/probe_446r.log") | $(grep SUMMARY "$O/control-0.1.14/probe_446r.log")"
"$W/venv/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_446r.py")" "$(cygpath -w "$W/work/446")" "$(cygpath -w "$O/probe_446r.json")" --baseline "$(cygpath -w "$O/control-0.1.14/probe_446r.json")" > "$O/probe_446r.log" 2>&1
echo "exit=$?" >> "$O/probe_446r.log"
echo "0.1.15 probe_446r $(tail -1 "$O/probe_446r.log") | $(grep SUMMARY "$O/probe_446r.log")"
echo "empty dir after: $(ls -A "$W/empty-446" | wc -l) entries"
