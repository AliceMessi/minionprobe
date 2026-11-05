#!/usr/bin/env bash
# Native Windows run of probe_541 and the consumer typing check (Git Bash).
#
#   bash run_541_windows.sh <work_dir> <probes_dir> <out_dir> <docs_runtime-surfaces.md>
#
# <work_dir> holds venv (openminion==0.1.17 from PyPI), venv0116 (==0.1.16), and
# for the typing check venv541t (openminion==0.1.17 + mypy>=1.10,<2) and
# venv541t16 (openminion==0.1.16 + mypy), all CPython 3.12.11 from uv. Outputs:
# <out_dir>/probe_541.{json,log}, typing*.txt, artifact-identity.txt,
# <out_dir>/control-0.1.16/..., <out_dir>/probe-sha256.txt.
set -u
W="$1"; P="$2"; O="$3"; DOC="$4"
if [[ ! -e "$W/PYPI_OK" ]]; then
  echo "refusing: $W/PYPI_OK does not exist"; exit 2
fi
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_TEST_POSTGRES_URL
mkdir -p "$O/control-0.1.16" "$W/empty-541"
rm -rf "$W/work/541" "$W/work/541-0116"
(cd "$P" && sha256sum _common.py probe_420_lib.py probe_524_lib.py probe_541.py probe_541_sdk_consumer_valid.py \
   probe_541_sdk_consumer_invalid.py probe_541_typing_example.py probe_541_typing_invalid_thread.py steps_541_typing.sh \
   run_541_windows.sh steps_541.sh; cd "$(dirname "$DOC")" && sha256sum "$(basename "$DOC")") > "$O/probe-sha256.txt"
cd "$W/empty-541" || exit 1
"$W/venv0116/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_541.py")" "$(cygpath -w "$W/work/541-0116")" "$(cygpath -w "$O/control-0.1.16/probe_541.json")" --doc "$(cygpath -w "$DOC")" --expect-version 0.1.16 > "$O/control-0.1.16/probe_541.log" 2>&1
echo "exit=$?" >> "$O/control-0.1.16/probe_541.log"
echo "0.1.16 probe_541 $(tail -1 "$O/control-0.1.16/probe_541.log") | $(grep SUMMARY "$O/control-0.1.16/probe_541.log")"
"$W/venv/Scripts/python.exe" -I -X utf8 "$(cygpath -w "$P/probe_541.py")" "$(cygpath -w "$W/work/541")" "$(cygpath -w "$O/probe_541.json")" --doc "$(cygpath -w "$DOC")" --baseline "$(cygpath -w "$O/control-0.1.16/probe_541.json")" > "$O/probe_541.log" 2>&1
echo "exit=$?" >> "$O/probe_541.log"
echo "0.1.17 probe_541 $(tail -1 "$O/probe_541.log") | $(grep SUMMARY "$O/probe_541.log")"
echo "empty dir after: $(ls -A "$W/empty-541" | wc -l) entries"

# Consumer typing. The typing venvs hold the same PyPI artifacts as venv/venv0116:
# their installed openminion files are compared with those venvs' files.
# shellcheck source=/dev/null
source "$P/steps_541_typing.sh"
for pair in "venv541t venv $O" "venv541t16 venv0116 $O/control-0.1.16"; do
  set -- $pair
  {
    echo "openminion files in $1 vs $2 (site-packages/openminion and the dist-info RECORD, __pycache__ excluded):"
    diff -r -x __pycache__ "$W/$1/Lib/site-packages/openminion" "$W/$2/Lib/site-packages/openminion" > /dev/null && echo "  identical" || echo "  DIFFERENT"
    # The console-script launchers (../../Scripts/*.exe) are generated per venv; every other RECORD line must match.
    diff <(grep -v '^\.\./\.\./Scripts/' "$W/$1/Lib/site-packages"/openminion-*.dist-info/RECORD)          <(grep -v '^\.\./\.\./Scripts/' "$W/$2/Lib/site-packages"/openminion-*.dist-info/RECORD) > /dev/null       && echo "  RECORD identical apart from the generated ../../Scripts/*.exe launchers ($(grep -vc '^\.\./\.\./Scripts/' "$W/$1/Lib/site-packages"/openminion-*.dist-info/RECORD) lines compared)"       || echo "  RECORD DIFFERENT"
    uv pip show --python "$W/$1/Scripts/python.exe" openminion mypy 2>&1 | grep -E '^(Name|Version)'
  } > "$3/artifact-identity.txt" 2>&1
done
typing_541 "$W/venv541t/Scripts/python.exe" "$P" "$W/work/541-typing" "$O" fixed
echo "0.1.17 typing exit=$?" | tee -a "$O/typing-summary.txt"
typing_541 "$W/venv541t16/Scripts/python.exe" "$P" "$W/work/541-typing-0116" "$O/control-0.1.16" control
echo "0.1.16 typing exit=$?" | tee -a "$O/control-0.1.16/typing-summary.txt"
