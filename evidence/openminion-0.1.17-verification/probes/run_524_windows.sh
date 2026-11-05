#!/usr/bin/env bash
# Native Windows run of probe_524 and probe_524_optional (built-in SQLite memory
# store): first the published 0.1.16 (the control), then the installed
# openminion==0.1.17 with the control's JSON as the baseline.
#
#   bash run_524_windows.sh <work_dir> <probes_dir> <out_dir>
#
# <work_dir> holds venv (openminion==0.1.17 from PyPI) and venv0116
# (openminion==0.1.16 from PyPI), both CPython 3.12.11. Outputs:
# <out_dir>/<probe>.{json,log}, <out_dir>/control-0.1.16/<probe>.{json,log} and
# <out_dir>/probe-sha256.txt. No PostgreSQL on Windows (the PostgreSQL cases run
# on the Linux runner, steps_524.sh).
set -u
W="$1"; P="$2"; O="$3"
if [[ ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_TEST_POSTGRES_URL OPENMINION_DISABLE_SECURITY_POLICY
mkdir -p "$O/control-0.1.16" "$W/empty-524"
(cd "$P" && sha256sum _common.py probe_420_lib.py probe_496_lib.py probe_524_lib.py probe_524.py probe_524_optional.py run_524_windows.sh steps_524.sh steps_524_optional.sh) > "$O/probe-sha256.txt"
cd "$W/empty-524" || exit 1
for probe in probe_524 probe_524_optional; do
  rm -rf "$W/work/$probe" "$W/work/$probe-0116"
  "$W/venv0116/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/$probe.py")" "$(cygpath -w "$W/work/$probe-0116")" "$(cygpath -w "$O/control-0.1.16/$probe.json")" --backend sqlite --expect-version 0.1.16 > "$O/control-0.1.16/$probe.log" 2>&1
  echo "exit=$?" >> "$O/control-0.1.16/$probe.log"
  echo "0.1.16 $probe $(tail -1 "$O/control-0.1.16/$probe.log") | $(grep SUMMARY "$O/control-0.1.16/$probe.log")"
  "$W/venv/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/$probe.py")" "$(cygpath -w "$W/work/$probe")" "$(cygpath -w "$O/$probe.json")" --backend sqlite --baseline "$(cygpath -w "$O/control-0.1.16/$probe.json")" > "$O/$probe.log" 2>&1
  echo "exit=$?" >> "$O/$probe.log"
  echo "0.1.17 $probe $(tail -1 "$O/$probe.log") | $(grep SUMMARY "$O/$probe.log")"
done
echo "empty dir after: $(ls -A "$W/empty-524" | wc -l) entries"
