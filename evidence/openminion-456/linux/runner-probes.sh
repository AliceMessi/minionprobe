#!/bin/bash
set -u
DEV="39c36a1b5183ca811a9031cbcb397b3f9470a06e"
HEAD_SHA="2145c61f14aa01bbcf4af4d2400f04d9a898ef10"
# The runner wrote probes/*.py byte for byte to /tmp/ev before this part (see probes.sha256).
# #456 PR evidence: the issue's probe and the fix probe on upstream dev and on the PR head,
# each installed from git into a fresh venv, run in isolated mode from an empty directory.
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH
mkdir -p /tmp/ev
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
(cd /tmp/ev && sha256sum *.py) | tee "$OUT/probes.sha256"
echo "$(grep PRETTY_NAME /etc/os-release)"
python -m pip install -q uv >/dev/null 2>&1
declare -A SPEC=( [dev]="openminion @ git+https://github.com/openminion/openminion@$DEV" [head]="openminion @ git+https://github.com/AliceMessi/openminion@$HEAD_SHA" )
for v in dev head; do
  echo "${SPEC[$v]}" | sed 's/.*@ //' > "$OUT/spec_$v.txt"
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv-$v"
  uv pip install -q --python "$RUNNER_TEMP/venv-$v/bin/python" "${SPEC[$v]}" || echo "install $v failed"
  "$RUNNER_TEMP/venv-$v/bin/python" -c 'import sys,sqlite3;print("venv '"$v"': CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'
  uv pip list --python "$RUNNER_TEMP/venv-$v/bin/python" > "$OUT/pip-list_$v.txt" 2>/dev/null
done
for v in dev head; do
  for probe in probe_consolidation_self_capture probe_consolidation_fix; do
    EMPTY="$RUNNER_TEMP/empty-$v-$probe"; mkdir -p "$EMPTY"
    echo; echo "=== $v $probe"
    (cd "$EMPTY" && "$RUNNER_TEMP/venv-$v/bin/python" -I -X utf8 /tmp/ev/$probe.py "$RUNNER_TEMP/work-$v-$probe" "$OUT/${probe}_$v.json" > "$OUT/${probe}_$v.log" 2>&1)
    echo "exit=$?"
    grep -E "^(PASS|FAIL|SUMMARY)" "$OUT/${probe}_$v.log" | cut -c1-260
  done
done
