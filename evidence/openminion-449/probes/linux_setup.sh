#!/usr/bin/env bash
# #449 PR evidence on the runner: the issue's probe (unchanged) and the PR probe on four
# installed trees, each installed from git into a fresh venv and run in isolated mode
# from an empty directory:
#   dev-96e8e662     upstream dev the PR commits are on (#456 not fixed)
#   pr-7be94543      the PR's three commits on it
#   dev-042e0a2c     upstream dev with #463 (fixes #456)
#   merged-34ccf868  the PR branch after merging dev-042e0a2c
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL
sha256sum "$B"/*.py | sed "s#$B/##" | tee "$OUT/probes.sha256"
python -m pip install -q uv >/dev/null 2>&1
TREES="dev-96e8e662 pr-7be94543 dev-042e0a2c merged-34ccf868"
declare -A SPEC=(
  [dev-96e8e662]="openminion @ git+https://github.com/openminion/openminion@96e8e6625ee7ea3f9ae501d3ff5559ea99e48242"
  [pr-7be94543]="openminion @ git+https://github.com/AliceMessi/openminion@7be94543d0b8361f7077cd6ee8e6a7d6ca75ee5e"
  [dev-042e0a2c]="openminion @ git+https://github.com/openminion/openminion@042e0a2c6d9efed679ec825ba621e1ee055be6af"
  [merged-34ccf868]="openminion @ git+https://github.com/AliceMessi/openminion@34ccf8689216a7895db0eb6b8f2f17eb48efae30"
)
for v in $TREES; do
  echo "${SPEC[$v]}" | sed 's/.*@ //' > "$OUT/spec_$v.txt"
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv-$v"
  uv pip install -q --python "$RUNNER_TEMP/venv-$v/bin/python" "${SPEC[$v]}" || echo "install $v failed"
done
for v in $TREES; do
  for probe in probe_unsupported_extraction probe_449_recovery; do
    EMPTY="$RUNNER_TEMP/empty-$v-$probe"; mkdir -p "$EMPTY"
    echo "=== $v $probe"
    (cd "$EMPTY" && "$RUNNER_TEMP/venv-$v/bin/python" -I -X utf8 "$B/$probe.py" "$RUNNER_TEMP/work-$v-$probe" "$OUT/${probe}_$v.json" > "$OUT/${probe}_$v.log" 2>&1)
    echo "exit=$?"
    grep -E "^SUMMARY|^PASS|^FAIL" "$OUT/${probe}_$v.log" | cut -c1-200
  done
done
echo ok > "$OUT/../result.txt" 2>/dev/null || true
