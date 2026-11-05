#!/usr/bin/env bash
# Native Windows runs for #456 on the installed openminion==0.1.17, with older published packages from PyPI in
# separate venvs: 0.1.14 (creates the pre-0.1.15 jobs) and 0.1.16 (the mixed-age home's job; the negative
# control).
#
#   bash run_456_windows.sh <W> <E>        (W: the work root with the venvs, E: the evidence folder)
#
# Each probe runs with `python -I -X utf8` from an empty directory outside any checkout. The guide's commands run
# through Git Bash (`bash -c`, the guide's POSIX syntax), with the package's Scripts directory first on PATH.
# The published guide is the byte copy E/release-source/published-docs-v0.1.17/docs_runtime-database-upgrades.md
# (the probes check its git blob, 0f6b9c63...).
set -u
W="$1"; E="$2"; P="$E/probes"; O="$E/windows/456"
if [[ ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_AGENT_ID
mkdir -p "$O/control-0.1.16" "$W/empty-456"
rm -rf "$W/work/456" "$W/work/456-0116" "$W/work/456int" "$W/work/456int-0116" "$W/work/456guide"
(cd "$P" && sha256sum _common.py probe_449_lib.py probe_449_ext.py probe_456.py probe_456_lib.py \
   probe_456_intents.py probe_456_guide.py run_456_windows.sh steps_456.sh) > "$O/probe-sha256.txt"
cd "$W/empty-456" || exit 1
win() { cygpath -w "$1"; }
V="$W/venv/Scripts"; V14="$W/venv0114/Scripts"; V16="$W/venv0116/Scripts"
GUIDE="$(win "$E/release-source/published-docs-v0.1.17/docs_runtime-database-upgrades.md")"
BASH_EXE='C:\Program Files\Git\bin\bash.exe'
"$BASH_EXE" --version | head -1 > "$O/shell-version.txt"
r() {  # r <out_dir> <name> <python> <args...>
  local o=$1 name=$2 py=$3; shift 3
  "$py" -I -X utf8 "$@" > "$o/$name.log" 2>&1
  echo "$name exit=$? | $(grep SUMMARY "$o/$name.log")"
}
r "$O" probe_456 "$V/python.exe" "$(win "$P/probe_456.py")" "$(win "$W/work/456")" "$(win "$O/probe_456.json")"
r "$O" probe_456_intents_create "$V14/python.exe" "$(win "$P/probe_456_intents.py")" create "$(win "$W/work/456int")" "$(win "$O/probe_456_intents_create.json")"
r "$O" probe_456_intents "$V/python.exe" "$(win "$P/probe_456_intents.py")" upgrade "$(win "$W/work/456int")" "$(win "$O/probe_456_intents.json")" "$GUIDE" "$BASH_EXE"
r "$O" probe_456_guide "$V/python.exe" "$(win "$P/probe_456_guide.py")" "$GUIDE" "$(win "$V14")" "$(win "$V16")" "$(win "$V")" "$BASH_EXE" "$(win "$W/work/456guide")" "$(win "$O/probe_456_guide.json")"
# negative control: the published 0.1.16 as the package under test
C="$O/control-0.1.16"
r "$C" probe_456 "$V16/python.exe" "$(win "$P/probe_456.py")" "$(win "$W/work/456-0116")" "$(win "$C/probe_456.json")"
r "$C" probe_456_intents_create "$V14/python.exe" "$(win "$P/probe_456_intents.py")" create "$(win "$W/work/456int-0116")" "$(win "$C/probe_456_intents_create.json")"
r "$C" probe_456_intents "$V16/python.exe" "$(win "$P/probe_456_intents.py")" upgrade "$(win "$W/work/456int-0116")" "$(win "$C/probe_456_intents.json")" "$GUIDE" "$BASH_EXE"
echo "empty dir after: $(ls -A "$W/empty-456" | wc -l) entries"
