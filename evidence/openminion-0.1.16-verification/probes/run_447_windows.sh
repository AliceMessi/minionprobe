#!/usr/bin/env bash
# Native Windows runs for #447 (SQLite; no PostgreSQL server on Windows, the
# PostgreSQL cases run on the Linux runner, steps_447.sh): first the published
# 0.1.15 (the control), then the installed openminion==0.1.16.
#
#   bash run_447_windows.sh <work_dir> <probes_dir> <out_dir>
#
# <work_dir> holds venv (openminion==0.1.16 from PyPI) and venv0115
# (openminion==0.1.15 from PyPI), both CPython 3.12.11. For each version:
#   probe_447.py --backend sqlite (0.1.16 with the control's JSON as baseline);
#   the #447 report's probes, unchanged: probe_447_report_retention.py and
#   probe_447_report_repro.py, and probe_447_report_checks.py over their outputs.
# Outputs: <out_dir>/*, <out_dir>/control-0.1.15/* and <out_dir>/probe-sha256.txt.
# Every probe runs with `python -I -X utf8` from an empty directory outside any
# checkout.
set -u
W="$1"; P="$2"; O="$3"
if [[ ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH \
      OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL OPENMINION_TEST_POSTGRES_URL \
      OPENMINION_MEMORY_PROVIDER PGURL
mkdir -p "$O/control-0.1.15" "$W/empty-447"
rm -rf "$W/work/447" "$W/work/447-0115"
mkdir -p "$W/work/447" "$W/work/447-0115"
(cd "$P" && sha256sum _common.py probe_447.py probe_447_report_retention.py probe_447_report_repro.py probe_447_report_checks.py run_447_windows.sh steps_447.sh) > "$O/probe-sha256.txt"
cd "$W/empty-447" || exit 1
win() { cygpath -w "$1"; }
for v in 0115 0116; do
  if [[ $v == 0115 ]]; then PYV="$W/venv0115/Scripts/python.exe"; D="$O/control-0.1.15"; WK="$W/work/447-0115"
  else PYV="$W/venv/Scripts/python.exe"; D="$O"; WK="$W/work/447"; fi
  BASE=()
  [[ $v == 0116 ]] && BASE=(--baseline "$(win "$O/control-0.1.15/probe_447.json")")
  "$PYV" -I -X utf8 "$(win "$P/probe_447.py")" "$(win "$WK/probe")" "$(win "$D/probe_447.json")" --backend sqlite "${BASE[@]}" > "$D/probe_447.log" 2>&1
  echo "exit=$?" >> "$D/probe_447.log"
  echo "$v probe_447 $(tail -1 "$D/probe_447.log") | $(grep SUMMARY "$D/probe_447.log")"
  "$PYV" -I -X utf8 "$(win "$P/probe_447_report_retention.py")" --label "win_v$v" --work "$(win "$WK/retention")" --out "$(win "$D/probe_447_report_retention.json")" > "$D/probe_447_report_retention.log" 2>&1
  echo "exit=$?" >> "$D/probe_447_report_retention.log"
  "$PYV" -I -X utf8 "$(win "$P/probe_447_report_repro.py")" "$(win "$WK/repro")" > "$D/probe_447_report_repro.txt" 2> "$D/probe_447_report_repro.stderr.txt"
  echo "$v report repro exit=$?: $(tr '\n' ' ' < "$D/probe_447_report_repro.txt")"
  "$PYV" -I -X utf8 "$(win "$P/probe_447_report_checks.py")" "$(win "$D/probe_447_report_retention.json")" "$(win "$D/probe_447_report_repro.txt")" "$(win "$D/probe_447_report_checks.json")" > "$D/probe_447_report_checks.log" 2>&1
  echo "exit=$?" >> "$D/probe_447_report_checks.log"
  echo "$v report checks $(tail -1 "$D/probe_447_report_checks.log") | $(grep SUMMARY "$D/probe_447_report_checks.log")"
done
echo "empty dir after: $(ls -A "$W/empty-447" | wc -l) entries"
