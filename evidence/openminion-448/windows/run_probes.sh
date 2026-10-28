#!/bin/bash
# #448 PR evidence on native Windows: the issue's probe (unchanged) and the PR probe on two
# installed trees, each from its own fresh uv venv (CPython 3.12.11) installed from git:
#   dev-042e0a2c   upstream dev the PR commits are on
#   pr-e8c7e811    the PR's three commits on it
#   dev-63ac9a17     upstream dev with #457
#   merged-8dc45d7b  the PR branch with a typing follow-up commit and a merge of dev-63ac9a17
set -u
W="${W:?set W to the work directory}"
E="${E:?set E to this evidence folder's probes directory}"
export VERIFY_WORK="$(cygpath -w "$W")"
declare -A SPEC=(
  [dev-042e0a2c]="openminion @ git+https://github.com/openminion/openminion@042e0a2c6d9efed679ec825ba621e1ee055be6af"
  [pr-e8c7e811]="openminion @ git+https://github.com/AliceMessi/openminion@e8c7e811982a715f2f994d2be988f7ff9d98c273"
  [dev-63ac9a17]="openminion @ git+https://github.com/openminion/openminion@63ac9a17c3f1d926072a1d474c052ef1f01de1b4"
  [merged-8dc45d7b]="openminion @ git+https://github.com/AliceMessi/openminion@8dc45d7b6560c18402e0d81bd0a9ac58cf2a70c8"
)
TREES="${TREES:-dev-042e0a2c pr-e8c7e811 dev-63ac9a17 merged-8dc45d7b}"
for tree in $TREES; do
  rm -rf "$W/venv-$tree"
  uv venv -q --python 3.12 "$W/venv-$tree" && uv pip install -q -p "$W/venv-$tree/Scripts/python.exe" "${SPEC[$tree]}" || echo "install $tree failed"
done
for tree in $TREES; do
  PY="$W/venv-$tree/Scripts/python.exe"
  OUT="$W/probe-out/$tree"; mkdir -p "$OUT"
  uv pip list -p "$PY" > "$OUT/pip-list.txt" 2>/dev/null
  "$PY" -c "from importlib import metadata as m; print(m.distribution('openminion').read_text('direct_url.json'))" > "$OUT/direct_url.json"
  for probe in probe_capture_authorization probe_448_turn_capture; do
    EMPTY="$W/empty-$tree-$probe"; rm -rf "$EMPTY" "$W/work-$tree-$probe"; mkdir -p "$EMPTY"
    echo "=== $tree $probe $(date -u +%H:%M:%SZ)"
    (cd "$EMPTY" && "$PY" -I -X utf8 "$(cygpath -w "$E/$probe.py")" "$(cygpath -w "$W/work-$tree-$probe")" "$(cygpath -w "$OUT/$probe.json")" > "$OUT/$probe.log" 2>&1)
    echo "exit=$?"
    grep -E "^SUMMARY|^PASS|^FAIL" "$OUT/$probe.log" | cut -c1-200
  done
done
echo "done $(date -u +%H:%M:%SZ)"
