#!/usr/bin/env bash
# Native Windows run of probe_448 (SQLite runtime storage and the built-in SQLite
# memory store): first the published 0.1.15 (control), then the installed
# openminion==0.1.16.
#
#   bash run_448_windows.sh <work_dir> <probes_dir> <out_dir>
#
# <work_dir> holds venv (openminion==0.1.16 from PyPI) and venv0115
# (openminion==0.1.15 from PyPI), both CPython 3.12.11. Outputs:
# <out_dir>/probe_448.{json,log}, <out_dir>/control-0.1.15/probe_448.{json,log}
# and <out_dir>/probe-sha256.txt. The probe runs with `python -I -X utf8` from
# an empty directory outside any checkout; no PostgreSQL on Windows (it runs on
# the Linux runner, steps_448.sh).
set -u
W="$1"; P="$2"; O="$3"
if [[ ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH \
      OPENMINION_TEST_POSTGRES_URL OPENMINION_STORAGE_POSTGRES_URL OPENMINION_DISABLE_SECURITY_POLICY PGURL
mkdir -p "$O/control-0.1.15" "$W/empty-448"
rm -rf "$W/work/448" "$W/work/448-0115"
(cd "$P" && sha256sum _common.py probe_420_lib.py probe_448.py probe_448_lib.py run_448_windows.sh steps_448.sh) > "$O/probe-sha256.txt"
cd "$W/empty-448" || exit 1
"$W/venv0115/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_448.py")" "$(cygpath -w "$W/work/448-0115")" "$(cygpath -w "$O/control-0.1.15/probe_448.json")" --backend sqlite > "$O/control-0.1.15/probe_448.log" 2>&1
echo "exit=$?" >> "$O/control-0.1.15/probe_448.log"
echo "0.1.15 probe_448 $(tail -1 "$O/control-0.1.15/probe_448.log") | $(grep SUMMARY "$O/control-0.1.15/probe_448.log")"
"$W/venv/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_448.py")" "$(cygpath -w "$W/work/448")" "$(cygpath -w "$O/probe_448.json")" --backend sqlite > "$O/probe_448.log" 2>&1
echo "exit=$?" >> "$O/probe_448.log"
echo "0.1.16 probe_448 $(tail -1 "$O/probe_448.log") | $(grep SUMMARY "$O/probe_448.log")"
echo "empty dir after: $(ls -A "$W/empty-448" | wc -l) entries"
