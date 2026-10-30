#!/usr/bin/env bash
# Native Windows (Git Bash): install openminion==0.1.16 from PyPI into a fresh
# venv (uv, CPython 3.12) and run probe_agent_run_confirmation_resume.py twice,
# each with a fresh work directory, with `python -I -X utf8` from an empty
# directory outside any checkout.
#   bash run_windows.sh <work_dir> <probes_dir> <out_dir>
# Outputs: <out_dir>/probe_runN.{json,log}, pip-install.log, pip-freeze.txt,
# probes.sha256.
set -u
W="$1"; P="$2"; O="$3"
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL
mkdir -p "$O" "$W/empty"
(cd "$P" && sha256sum *.py *.sh) > "$O/probes.sha256"
uv venv -q --python 3.12 "$W/venv"
uv pip install -q --python "$W/venv/Scripts/python.exe" "openminion==0.1.16" > "$O/pip-install.log" 2>&1
echo "pip install exit=$?"
uv pip freeze --python "$W/venv/Scripts/python.exe" > "$O/pip-freeze.txt" 2>/dev/null
cd "$W/empty" || exit 1
for i in 1 2; do
  "$W/venv/Scripts/python.exe" -B -I -X utf8 "$(cygpath -w "$P/probe_agent_run_confirmation_resume.py")" \
    "$(cygpath -w "$W/work-$i")" "$(cygpath -w "$O/probe_run$i.json")" --expect-version 0.1.16 > "$O/probe_run$i.log" 2>&1
  echo "run $i exit=$?"
  grep -E "^SUMMARY" "$O/probe_run$i.log"
done
echo "empty dir after: $(ls -A "$W/empty" | wc -l) entries"
