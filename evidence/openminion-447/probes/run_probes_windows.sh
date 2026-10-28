#!/bin/bash
# #447 PR evidence on native Windows: the report's probe (unchanged) and the PR probe
# (SQLite cases) on two installed trees, each from its own fresh uv venv (CPython 3.12)
# installed from git:
#   dev-042e0a2c   upstream dev the PR commits are on
#   pr-<sha>       the PR branch head
set -u
W=<WORK>
P=$W/probes
TREES="${TREES:-dev-042e0a2c pr-20034752}"
CASES="control memory-provider sessions-table engine-emitter storage-migration extensions ops support-authoring after-support gateway connection post-init runtime-manager bind close-fails"
for tree in $TREES; do
  PY="$W/venv-$tree/Scripts/python.exe"
  OUT="$W/probe-out/windows/$tree"; rm -rf "$OUT"; mkdir -p "$OUT"
  uv pip list -p "$PY" > "$OUT/pip-list.txt" 2>/dev/null
  "$PY" -c "from importlib import metadata as m; print(m.distribution('openminion').read_text('direct_url.json'))" > "$OUT/direct_url.json"
  EMPTY="$W/empty-$tree"; rm -rf "$EMPTY" "$W/work-$tree"; mkdir -p "$EMPTY" "$W/work-$tree"
  echo "=== $tree probe_failed_start_retention $(date -u +%H:%M:%SZ)"
  (cd "$EMPTY" && "$PY" -I -X utf8 "$(cygpath -w "$P/probe_failed_start_retention.py")" --label "win_$tree" \
     --work "$(cygpath -w "$W/work-$tree/retention")" --out "$(cygpath -w "$OUT/probe_failed_start_retention.json")" \
     > "$OUT/probe_failed_start_retention.log" 2>&1)
  echo "exit=$?"
  grep -E "^CASE" "$OUT/probe_failed_start_retention.log" | cut -c1-300
  for c in $CASES; do
    (cd "$EMPTY" && "$PY" -I -X utf8 "$(cygpath -w "$P/probe_447_boundaries.py")" "$c" "$(cygpath -w "$W/work-$tree/$c")" \
       > "$OUT/boundaries-$c.json" 2> "$OUT/boundaries-$c.stderr.txt")
    echo "$tree $c exit=$?"
  done
done
echo "done $(date -u +%H:%M:%SZ)"
