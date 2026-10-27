#!/bin/bash
# #419 on native Windows (Git Bash). Required variables:
#   W  work root holding venv (openminion==0.1.13 from PyPI), venv0112
#      (openminion==0.1.12 from PyPI) and the empty directory empty-419
#   E  this evidence folder (openminion-0.1.13-verification)
# Every probe runs with `python -I -B -X utf8` from the empty directory, outside any checkout.
set -u
: "${W:?}" "${E:?}"
OUTW="$E/windows/419"
mkdir -p "$OUTW/control-0.1.12" "$W/empty-419"
rm -rf "$W/work/419" "$W/work/419-upgrade" "$W/work/419-0112"
cd "$W/empty-419" || exit 1
export VERIFY_WORK="$(cygpath -w "$W")"
P13="$W/venv/Scripts/python.exe"
P12="$W/venv0112/Scripts/python.exe"
(cd "$E/probes" && sha256sum _common.py probe_419.py probe_419_lib.py probe_419_upgrade.py run_419_windows.sh) > "$OUTW/probe-sha256.txt"
"$P13" -I -B -X utf8 "$E/probes/probe_419.py" "$(cygpath -w "$W/work/419")" "$(cygpath -w "$OUTW/probe_419.json")" > "$OUTW/probe_419.log" 2>&1
echo "probe_419 exit=$?" | tee -a "$OUTW/probe_419.log"
"$P13" -I -B -X utf8 "$E/probes/probe_419_upgrade.py" "$(cygpath -w "$P12")" "$(cygpath -w "$W/work/419-upgrade")" "$(cygpath -w "$OUTW/probe_419_upgrade.json")" > "$OUTW/probe_419_upgrade.log" 2>&1
echo "probe_419_upgrade exit=$?" | tee -a "$OUTW/probe_419_upgrade.log"
"$P12" -I -B -X utf8 "$E/probes/probe_419.py" "$(cygpath -w "$W/work/419-0112")" "$(cygpath -w "$OUTW/control-0.1.12/probe_419.json")" > "$OUTW/control-0.1.12/probe_419.log" 2>&1
echo "probe_419 (0.1.12 control) exit=$?" | tee -a "$OUTW/control-0.1.12/probe_419.log"
grep -h "^SUMMARY" "$OUTW/probe_419.log" "$OUTW/probe_419_upgrade.log" "$OUTW/control-0.1.12/probe_419.log"
