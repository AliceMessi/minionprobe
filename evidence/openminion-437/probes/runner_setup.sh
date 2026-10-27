#!/usr/bin/env bash
# #437 PR evidence: the two probes on upstream dev and on the PR head, each installed
# from git into a fresh venv, run in isolated mode from an empty directory.
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL
export PGURL="${OPENMINION_TEST_POSTGRES_URL:-}"
sha256sum "$B"/*.py | sed "s#$B/##" | tee "$OUT/probes.sha256"
python -m pip install -q uv >/dev/null 2>&1
declare -A SPEC=( [dev]="openminion[postgres] @ git+https://github.com/openminion/openminion@691a1d543822522bc7208d45dd3be9e602b8e5ce" [head]="openminion[postgres] @ git+https://github.com/AliceMessi/openminion@3ad2981cc2830e2900f5b515a978e252284f8b94" )
for v in dev head; do
  echo "${SPEC[$v]}" | sed 's/.*@ //' > "$OUT/spec_$v.txt"
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv-$v"
  uv pip install -q --python "$RUNNER_TEMP/venv-$v/bin/python" "${SPEC[$v]}" || echo "install $v failed"
done
for v in dev head; do
  for probe in probe_apiruntime_pg probe_runtime_postgres_data; do
    EMPTY="$RUNNER_TEMP/empty-$v-$probe"; mkdir -p "$EMPTY"
    echo "=== $v $probe"
    (cd "$EMPTY" && PATH="$RUNNER_TEMP/venv-$v/bin:$PATH" "$RUNNER_TEMP/venv-$v/bin/python" -I -X utf8 "$B/$probe.py" --label "$v" --work "$RUNNER_TEMP/work-$v-$probe" --out "$OUT/${probe}_$v.json" > "$OUT/${probe}_$v.log" 2>&1)
    echo "exit=$?"
    grep -E "^CASE|^PROBE" "$OUT/${probe}_$v.log" | cut -c1-600
  done
done
echo ok > "$OUT/../result.txt" 2>/dev/null || true
