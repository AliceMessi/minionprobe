#!/bin/bash
# probe_rollback_companions.py (round 3) on native Windows (Git Bash), for each package under test.
# Required variables:
#   W  work root holding the venvs and the empty directory `empty`:
#        venv13    openminion==0.1.13 from PyPI
#        venvhead  the PR head 2d8fd446f699c1c96345607fbcdb475c9ec85942 (dev @ 4083b663 plus
#                  the docs change), `uv pip install "openminion @ git+https://github.com/AliceMessi/openminion@<sha>"`
#   E  this folder (round3)
# The probe runs with `python -I -X utf8` from the empty directory, outside any checkout.
set -u
: "${W:?}" "${E:?}"
cd "$W/empty" || exit 1
export VERIFY_WORK="$(cygpath -w "$W")"
(cd "$E/probes" && sha256sum _common.py probe_rollback_companions.py run_windows.sh) > "$E/windows/probe-sha256.txt"
for pair in "0.1.13:venv13" "head-2d8fd446:venvhead"; do
  label=${pair%%:*}; venv=${pair#*:}
  O="$E/windows/$label"; mkdir -p "$O"
  PY="$W/$venv/Scripts/python.exe"
  uv pip list --python "$PY" > "$O/pip-list.txt" 2>/dev/null
  "$PY" -I -B -X utf8 "$E/probes/probe_rollback_companions.py" "$(cygpath -w "$W/work/$label/companions")" \
    "$(cygpath -w "$O/probe_rollback_companions.json")" > "$O/probe_rollback_companions.log" 2>&1
  echo "probe_rollback_companions exit=$?" | tee -a "$O/probe_rollback_companions.log"
  grep -h "^SUMMARY" "$O"/*.log
done
