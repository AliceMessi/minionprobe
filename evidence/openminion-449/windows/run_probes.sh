#!/bin/bash
# #449 PR evidence on native Windows: the issue's probe (unchanged) and the PR probe on four
# installed trees, each from its own fresh uv venv (CPython 3.12.11) installed from git:
#   dev-96e8e662     upstream dev the PR commits are on (#456 not fixed)
#   pr-7be94543      the PR's three commits on it
#   dev-042e0a2c     upstream dev with #463 (fixes #456)
#   merged-34ccf868  the PR branch after merging dev-042e0a2c
set -u
W=<WORK>  # work dir: venv-<tree> (uv venv --python 3.12.11; uv pip install "openminion @ git+..."), outputs
E=<EVIDENCE>/probes  # this folder's probes/
export VERIFY_WORK="$(cygpath -w "$W")"
for tree in dev-96e8e662 pr-7be94543 dev-042e0a2c merged-34ccf868; do
  PY="$W/venv-$tree/Scripts/python.exe"
  OUT="$W/probe-out/$tree"; mkdir -p "$OUT"
  uv pip list -p "$PY" > "$OUT/pip-list.txt" 2>/dev/null
  "$PY" -c "from importlib import metadata as m; print(m.distribution('openminion').read_text('direct_url.json'))" > "$OUT/direct_url.json"
  for probe in probe_unsupported_extraction probe_449_recovery; do
    EMPTY="$W/empty-$tree-$probe"; rm -rf "$EMPTY" "$W/work-$tree-$probe"; mkdir -p "$EMPTY"
    echo "=== $tree $probe $(date -u +%H:%M:%SZ)"
    (cd "$EMPTY" && "$PY" -I -X utf8 "$(cygpath -w "$E/$probe.py")" "$(cygpath -w "$W/work-$tree-$probe")" "$(cygpath -w "$OUT/$probe.json")" > "$OUT/$probe.log" 2>&1)
    echo "exit=$?"
    grep -E "^SUMMARY|^PASS|^FAIL" "$OUT/$probe.log" | cut -c1-200
  done
done
echo "done $(date -u +%H:%M:%SZ)"
