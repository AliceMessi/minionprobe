#!/usr/bin/env bash
# Native Windows runs for #456 on the installed openminion==0.1.15, with the
# published 0.1.14 as the negative control.
#
#   bash run_456_windows.sh <work_dir> <probes_dir> <out_dir>
#
# probe_456 (new jobs): 0.1.14 into control-0.1.14/, 0.1.15 into <out_dir>.
# probe_456_upgrade (pre-upgrade jobs): "create" with 0.1.14, then "upgrade"
# of the same runtime homes with 0.1.15 (into <out_dir>); the control repeats
# "create" with 0.1.14 in a second work directory and "upgrade" with 0.1.14
# (into control-0.1.14/). Each probe runs with `python -I -X utf8` from an
# empty directory outside any checkout.
set -u
W="$1"; P="$2"; O="$3"
if [[ ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_TEST_POSTGRES_URL
mkdir -p "$O/control-0.1.14" "$W/empty-456"
rm -rf "$W/work/456" "$W/work/456-0114" "$W/work/456up" "$W/work/456up-0114"
(cd "$P" && sha256sum _common.py probe_449_lib.py probe_449_ext.py probe_456.py probe_456_lib.py probe_456_upgrade.py run_456_windows.sh steps_456.sh) > "$O/probe-sha256.txt"
cd "$W/empty-456" || exit 1
win() { cygpath -w "$1"; }
V15="$W/venv/Scripts/python.exe"; V14="$W/venv0114/Scripts/python.exe"
"$V14" -I -X utf8 "$(win "$P/probe_456.py")" "$(win "$W/work/456-0114")" "$(win "$O/control-0.1.14/probe_456.json")" > "$O/control-0.1.14/probe_456.log" 2>&1
echo "0.1.14 probe_456 exit=$? | $(grep SUMMARY "$O/control-0.1.14/probe_456.log")"
"$V15" -I -X utf8 "$(win "$P/probe_456.py")" "$(win "$W/work/456")" "$(win "$O/probe_456.json")" > "$O/probe_456.log" 2>&1
echo "0.1.15 probe_456 exit=$? | $(grep SUMMARY "$O/probe_456.log")"
"$V14" -I -X utf8 "$(win "$P/probe_456_upgrade.py")" create "$(win "$W/work/456up")" "$(win "$O/probe_456_upgrade_create.json")" > "$O/probe_456_upgrade_create.log" 2>&1
echo "0.1.14 probe_456_upgrade create exit=$? | $(grep SUMMARY "$O/probe_456_upgrade_create.log")"
"$V15" -I -X utf8 "$(win "$P/probe_456_upgrade.py")" upgrade "$(win "$W/work/456up")" "$(win "$O/probe_456_upgrade.json")" > "$O/probe_456_upgrade.log" 2>&1
echo "0.1.15 probe_456_upgrade upgrade exit=$? | $(grep SUMMARY "$O/probe_456_upgrade.log")"
"$V14" -I -X utf8 "$(win "$P/probe_456_upgrade.py")" create "$(win "$W/work/456up-0114")" "$(win "$O/control-0.1.14/probe_456_upgrade_create.json")" > "$O/control-0.1.14/probe_456_upgrade_create.log" 2>&1
echo "0.1.14 probe_456_upgrade create (control) exit=$? | $(grep SUMMARY "$O/control-0.1.14/probe_456_upgrade_create.log")"
"$V14" -I -X utf8 "$(win "$P/probe_456_upgrade.py")" upgrade "$(win "$W/work/456up-0114")" "$(win "$O/control-0.1.14/probe_456_upgrade.json")" > "$O/control-0.1.14/probe_456_upgrade.log" 2>&1
echo "0.1.14 probe_456_upgrade upgrade (control) exit=$? | $(grep SUMMARY "$O/control-0.1.14/probe_456_upgrade.log")"
echo "empty dir after: $(ls -A "$W/empty-456" | wc -l) entries"
