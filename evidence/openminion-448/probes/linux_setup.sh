#!/usr/bin/env bash
# #448 PR evidence on the runner: the issue's probe (unchanged) and the PR probe on two
# installed trees, each installed from git into a fresh venv and run in isolated mode
# from an empty directory:
#   dev-042e0a2c     upstream dev the PR commits are on
#   pr-e8c7e811      the PR's three commits on it
#   dev-63ac9a17     upstream dev with #457
#   merged-8dc45d7b  the PR branch with a typing follow-up commit and a merge of dev-63ac9a17
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL
sha256sum "$B"/*.py | sed "s#$B/##" | tee "$OUT/probes.sha256"
python -m pip install -q uv >/dev/null 2>&1
TREES="dev-042e0a2c pr-e8c7e811 dev-63ac9a17 merged-8dc45d7b"
declare -A SPEC=(
  [dev-042e0a2c]="openminion @ git+https://github.com/openminion/openminion@042e0a2c6d9efed679ec825ba621e1ee055be6af"
  [pr-e8c7e811]="openminion @ git+https://github.com/AliceMessi/openminion@e8c7e811982a715f2f994d2be988f7ff9d98c273"
  [dev-63ac9a17]="openminion @ git+https://github.com/openminion/openminion@63ac9a17c3f1d926072a1d474c052ef1f01de1b4"
  [merged-8dc45d7b]="openminion @ git+https://github.com/AliceMessi/openminion@8dc45d7b6560c18402e0d81bd0a9ac58cf2a70c8"
)
for v in $TREES; do
  echo "${SPEC[$v]}" | sed 's/.*@ //' > "$OUT/spec_$v.txt"
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv-$v"
  uv pip install -q --python "$RUNNER_TEMP/venv-$v/bin/python" "${SPEC[$v]}" || echo "install $v failed"
  uv pip list --python "$RUNNER_TEMP/venv-$v/bin/python" > "$OUT/pip-list_$v.txt" 2>/dev/null
done
for v in $TREES; do
  for probe in probe_capture_authorization probe_448_turn_capture; do
    EMPTY="$RUNNER_TEMP/empty-$v-$probe"; mkdir -p "$EMPTY"
    echo "=== $v $probe"
    (cd "$EMPTY" && "$RUNNER_TEMP/venv-$v/bin/python" -I -X utf8 "$B/$probe.py" "$RUNNER_TEMP/work-$v-$probe" "$OUT/${probe}_$v.json" > "$OUT/${probe}_$v.log" 2>&1)
    echo "exit=$?"
    grep -E "^SUMMARY|^PASS|^FAIL" "$OUT/${probe}_$v.log" | cut -c1-200
  done
done
