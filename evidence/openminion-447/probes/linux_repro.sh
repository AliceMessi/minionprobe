#!/usr/bin/env bash
# #447 PR evidence on the runner: the report's minimal reproduction, unchanged, with SQLite
# runtime storage and OPENMINION_MEMORY_PROVIDER=not-a-provider, on two trees installed from
# git into fresh venvs (uv, Python 3.12):
#   dev  openminion[postgres] @ git+https://github.com/openminion/openminion@$DEV_SHA
#   pr   openminion[postgres] @ git+https://github.com/AliceMessi/openminion@$PR_SHA
# (Its PostgreSQL mode used the #437 config, which starts on both trees since #458.)
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"
mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH \
  OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL OPENMINION_MEMORY_PROVIDER
python -m pip install -q uv >/dev/null 2>&1
declare -A SPEC=(
  [dev]="openminion[postgres] @ git+https://github.com/openminion/openminion@$DEV_SHA"
  [pr]="openminion[postgres] @ git+https://github.com/AliceMessi/openminion@$PR_SHA"
)
(cd "$B" && sha256sum repro_failed_start.py linux_repro.sh) > "$OUT/repro-sha256.txt"
for v in dev pr; do
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv-$v"
  uv pip install -q --python "$RUNNER_TEMP/venv-$v/bin/python" --index-url https://pypi.org/simple "${SPEC[$v]}" \
    || echo "install $v failed" | tee -a "$OUT/install-errors.txt"
  uv pip list --python "$RUNNER_TEMP/venv-$v/bin/python" > "$OUT/repro-pip-list-$v.txt" 2>&1
  EMPTY="$RUNNER_TEMP/empty-$v"; mkdir -p "$EMPTY"
  echo "=== $v"
  (cd "$EMPTY" && "$RUNNER_TEMP/venv-$v/bin/python" -I -X utf8 "$B/repro_failed_start.py" "$RUNNER_TEMP/work-$v" \
     > "$OUT/repro-failed-start-sqlite-$v.txt" 2> "$OUT/repro-failed-start-sqlite-$v.stderr.txt")
  echo "exit=$?"
  cat "$OUT/repro-failed-start-sqlite-$v.txt"
done
echo ok > "$RUNNER_TEMP/logs/result.txt" 2>/dev/null || true
