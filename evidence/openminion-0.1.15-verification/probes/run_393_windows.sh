#!/usr/bin/env bash
# Native Windows run of probe_393 (SQLite): first the published 0.1.14
# (negative control), then the installed openminion==0.1.15.
#
#   bash run_393_windows.sh <work_dir> <probes_dir> <out_dir>
#
# <work_dir> holds venv (openminion==0.1.15 from PyPI) and venv0114
# (openminion==0.1.14 from PyPI), both CPython 3.12.11. Outputs:
# <out_dir>/probe_393.{json,log}, <out_dir>/control-0.1.14/probe_393.{json,log}
# and <out_dir>/probe-sha256.txt. The probe runs with `python -I -X utf8` from
# an empty directory outside any checkout; no PostgreSQL on Windows (it runs on
# the Linux runner, steps_393.sh).
set -u
W="$1"; P="$2"; O="$3"
if [[ ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_TEST_POSTGRES_URL
mkdir -p "$O/control-0.1.14" "$W/empty-393"
rm -rf "$W/work/393" "$W/work/393-0114"
(cd "$P" && sha256sum _common.py probe_393.py probe_393_lib.py run_393_windows.sh steps_393.sh) > "$O/probe-sha256.txt"
cd "$W/empty-393" || exit 1
"$W/venv0114/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_393.py")" "$(cygpath -w "$W/work/393-0114")" "$(cygpath -w "$O/control-0.1.14/probe_393.json")" --backend sqlite --rooms 200 > "$O/control-0.1.14/probe_393.log" 2>&1
echo "exit=$?" >> "$O/control-0.1.14/probe_393.log"
echo "0.1.14 probe_393 $(tail -1 "$O/control-0.1.14/probe_393.log") | $(grep SUMMARY "$O/control-0.1.14/probe_393.log")"
"$W/venv/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_393.py")" "$(cygpath -w "$W/work/393")" "$(cygpath -w "$O/probe_393.json")" --backend sqlite --rooms 200 > "$O/probe_393.log" 2>&1
echo "exit=$?" >> "$O/probe_393.log"
echo "0.1.15 probe_393 $(tail -1 "$O/probe_393.log") | $(grep SUMMARY "$O/probe_393.log")"
echo "empty dir after: $(ls -A "$W/empty-393" | wc -l) entries"
