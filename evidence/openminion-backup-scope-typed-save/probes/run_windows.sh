#!/bin/bash
# Both probes on native Windows (Git Bash), for each package under test.
# Required variables:
#   W  work root holding the venvs and the empty directory `empty`:
#        venv13   openminion==0.1.13 from PyPI
#        venv12   openminion==0.1.12 from PyPI (seeds the old working state)
#        venvdev  dev @ 68d623d10317a7cea13832f8dbd51fc98a871aa5 (`git archive` of that
#                 commit, installed with `uv pip install <dir>`)
#   E  this evidence folder
# Every probe runs with `python -I -X utf8` from the empty directory, outside any checkout.
set -u
: "${W:?}" "${E:?}"
cd "$W/empty" || exit 1
export VERIFY_WORK="$(cygpath -w "$W")"
P12="$W/venv12/Scripts/python.exe"
(cd "$E/probes" && sha256sum _common.py probe_backup_scope.py probe_typed_save_boundary.py run_windows.sh) > "$E/windows/probe-sha256.txt"
for pair in "0.1.13:venv13" "dev-68d623d1:venvdev"; do
  label=${pair%%:*}; venv=${pair#*:}
  O="$E/windows/$label"; mkdir -p "$O"
  PY="$W/$venv/Scripts/python.exe"
  uv pip list --python "$PY" > "$O/pip-list.txt" 2>/dev/null
  "$PY" -I -B -X utf8 "$E/probes/probe_backup_scope.py" "$(cygpath -w "$W/work/$label/backup")" \
    "$(cygpath -w "$O/probe_backup_scope.json")" > "$O/probe_backup_scope.log" 2>&1
  echo "probe_backup_scope exit=$?" | tee -a "$O/probe_backup_scope.log"
  "$PY" -I -B -X utf8 "$E/probes/probe_typed_save_boundary.py" "$(cygpath -w "$P12")" \
    "$(cygpath -w "$W/work/$label/typed")" "$(cygpath -w "$O/probe_typed_save_boundary.json")" \
    > "$O/probe_typed_save_boundary.log" 2>&1
  echo "probe_typed_save_boundary exit=$?" | tee -a "$O/probe_typed_save_boundary.log"
  grep -h "^SUMMARY" "$O/probe_backup_scope.log" "$O/probe_typed_save_boundary.log"
done
uv pip show --python "$P12" openminion > "$E/windows/pip-show-0.1.12.txt" 2>/dev/null
