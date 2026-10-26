#!/usr/bin/env bash
# #396 on native Windows (SQLite), Git Bash: the published 0.1.12 and, as the
# negative control, 0.1.11 (fresh venvs from PyPI, openminion[acp]; see steps_369_windows.sh).
set -uo pipefail
# W and E were absolute local paths when this ran; before publishing they were replaced by
# these two variables (no other change).
W="${W:?set W to the work folder}"
E="${E:?set E to this evidence folder}"
P=$E/probes; O=$E/windows/396; C=$O/control-0.1.11; D=$W/work/396
PY=$W/venv369/Scripts/python.exe; PY0111=$W/venv369-0111/Scripts/python.exe
wp() { cygpath -w "$1"; }
export VERIFY_WORK; VERIFY_WORK=$(wp "$W")
rm -rf "$D"; mkdir -p "$D" "$O" "$C" "$W/empty-396"
cd "$W/empty-396"
"$PY" -c "import sys,sqlite3,openminion;print('openminion',openminion.__version__,'python',sys.version.split()[0],'sqlite',sqlite3.sqlite_version)" > "$O/versions.txt"
"$PY0111" -c "import sys,sqlite3,openminion;print('openminion',openminion.__version__,'python',sys.version.split()[0],'sqlite',sqlite3.sqlite_version)" >> "$O/versions.txt"
echo "=== probe_396 (0.1.12): python -I -X utf8 probes/probe_396.py <work> probe_396.json" > "$O/probe_396.log"
"$PY" -I -X utf8 "$P/probe_396.py" "$(wp "$D/v0112")" "$(wp "$O/probe_396.json")" >> "$O/probe_396.log" 2>&1
echo "exit=$?" >> "$O/probe_396.log"
echo "=== probe_396 (0.1.11 control): python -I -X utf8 probes/probe_396.py <work> probe_396.json" > "$C/probe_396.log"
"$PY0111" -I -X utf8 "$P/probe_396.py" "$(wp "$D/v0111")" "$(wp "$C/probe_396.json")" >> "$C/probe_396.log" 2>&1
echo "exit=$?" >> "$C/probe_396.log"
grep -h -E "^(SUMMARY|exit=)" "$O/probe_396.log" "$C/probe_396.log"
(cd "$P" && sha256sum probe_396.py probe_369_lib.py _common.py steps_396_windows.sh) > "$O/probe-sha256.txt"
