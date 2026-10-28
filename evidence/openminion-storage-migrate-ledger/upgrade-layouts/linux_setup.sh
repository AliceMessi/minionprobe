#!/usr/bin/env bash
# #446 upgrade impact: probe_446_layouts.py on the published 0.1.13 and on dev 988ac4ec.
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL
python -m pip install -q uv >/dev/null 2>&1
declare -A SPEC=( [v0113]="openminion[postgres]==0.1.13" [dev]="openminion[postgres] @ git+https://github.com/openminion/openminion@988ac4ec54ae803f4ac937a6fc2fc1c04bb5f590" )
for v in v0113 dev; do
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv-$v"
  uv pip install -q --python "$RUNNER_TEMP/venv-$v/bin/python" "${SPEC[$v]}" || echo "install $v failed"
done
for v in v0113 dev; do
  EMPTY="$RUNNER_TEMP/empty-$v"; mkdir -p "$EMPTY"
  echo "=== $v $("$RUNNER_TEMP/venv-$v/bin/python" -c 'import importlib.metadata as m; print(m.version("openminion"))')"
  (cd "$EMPTY" && "$RUNNER_TEMP/venv-$v/bin/python" -I -X utf8 "$B/probe_446_layouts.py" > "$OUT/layouts_$v.log" 2>&1)
  echo "exit=$?"
  cat "$OUT/layouts_$v.log"
done
