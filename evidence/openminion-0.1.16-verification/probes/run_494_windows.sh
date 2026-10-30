#!/usr/bin/env bash
# Native Windows run of probe_494 (#494, SQLite only: item 4; PostgreSQL runs on
# the Linux runner, steps_494.sh): first the installed openminion==0.1.16 with
# the published 0.1.15 as --previous-python (its console script migrates the
# comparison files), then the same probe on 0.1.15 alone as the control.
#
#   bash run_494_windows.sh <work_dir> <probes_dir> <out_dir>
#
# <work_dir> holds venv (openminion==0.1.16 from PyPI) and venv0115
# (openminion==0.1.15 from PyPI), both CPython 3.12.11. Outputs:
# <out_dir>/probe_494_sqlite.{json,log}, <out_dir>/control-0.1.15/probe_494_sqlite.{json,log}
# and <out_dir>/probe-sha256.txt. The probe runs with `python -I -X utf8` from an
# empty directory outside any checkout.
set -u
W="$1"; P="$2"; O="$3"
if [[ ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH \
      OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL OPENMINION_TEST_POSTGRES_URL PGURL
mkdir -p "$O/control-0.1.15" "$W/empty-494"
rm -rf "$W/work/494" "$W/work/494-0115"
(cd "$P" && sha256sum _common.py probe_494.py run_494_windows.sh steps_494.sh) > "$O/probe-sha256.txt"
cd "$W/empty-494" || exit 1
win() { cygpath -w "$1"; }
V16="$W/venv/Scripts/python.exe"; V15="$W/venv0115/Scripts/python.exe"
"$V16" -I -X utf8 "$(win "$P/probe_494.py")" "$(win "$W/work/494")" "$(win "$O/probe_494_sqlite.json")" --backend sqlite --previous-python "$(win "$V15")" > "$O/probe_494_sqlite.log" 2>&1
echo "exit=$?" >> "$O/probe_494_sqlite.log"
echo "0.1.16 probe_494 sqlite $(tail -1 "$O/probe_494_sqlite.log") | $(grep SUMMARY "$O/probe_494_sqlite.log")"
"$V15" -I -X utf8 "$(win "$P/probe_494.py")" "$(win "$W/work/494-0115")" "$(win "$O/control-0.1.15/probe_494_sqlite.json")" --backend sqlite > "$O/control-0.1.15/probe_494_sqlite.log" 2>&1
echo "exit=$?" >> "$O/control-0.1.15/probe_494_sqlite.log"
echo "0.1.15 probe_494 sqlite $(tail -1 "$O/control-0.1.15/probe_494_sqlite.log") | $(grep SUMMARY "$O/control-0.1.15/probe_494_sqlite.log")"
echo "empty dir after: $(ls -A "$W/empty-494" | wc -l) entries"
