#!/usr/bin/env bash
# Runner job for the two minimal reproductions (AliceMessi/linux-runner, preset "none",
# PostgreSQL 16 in Docker at $OPENMINION_TEST_POSTGRES_URL).
# Two new venvs: dev @ 68d623d1 (git) and openminion[postgres]==0.1.13 (PyPI). For each:
#   repro_storage_migrate.sh          the storage-migrate-ledger reproduction
#   repro_failed_start.py <dir>       the failed-start reproduction, SQLite
#   repro_failed_start.py <dir> <url> the same with storage.backend: postgres
# Outputs go to $RUNNER_TEMP/logs/out.
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"
mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH \
  OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL OPENMINION_MEMORY_PROVIDER
export PGURL="${OPENMINION_TEST_POSTGRES_URL:-}"
DEV_SHA=68d623d10317a7cea13832f8dbd51fc98a871aa5
python -m pip install -q uv >/dev/null 2>&1
declare -A SPEC=(
  [dev]="openminion[postgres] @ git+https://github.com/openminion/openminion@$DEV_SHA"
  [0.1.13]="openminion[postgres]==0.1.13"
)
(cd "$B" && sha256sum repro_storage_migrate.sh repro_failed_start.py linux_setup.sh) > "$OUT/repro-sha256.txt"
for v in dev 0.1.13; do
  V="$RUNNER_TEMP/venv-$v"
  uv venv -q --python 3.12 "$V"
  uv pip install -q --python "$V/bin/python" --index-url https://pypi.org/simple "${SPEC[$v]}" || echo "install $v failed"
  uv pip list --python "$V/bin/python" > "$OUT/repro-pip-list-$v.txt" 2>&1
  EMPTY="$RUNNER_TEMP/repro-empty-$v"; mkdir -p "$EMPTY"
  label=$(echo "$v" | tr -c 'a-z0-9\n' '_')
  (cd "$EMPTY" && PATH="$V/bin:$PATH" bash "$B/repro_storage_migrate.sh" "$label") > "$OUT/repro-storage-migrate-$v.txt" 2>&1
  (cd "$EMPTY" && "$V/bin/python" -I -X utf8 "$B/repro_failed_start.py" "$RUNNER_TEMP/repro-sqlite-$v") \
    > "$OUT/repro-failed-start-sqlite-$v.txt" 2> "$OUT/repro-failed-start-sqlite-$v.stderr.txt"
  echo "exit=$?" >> "$OUT/repro-failed-start-sqlite-$v.txt"
  PGDB=$(cd "$EMPTY" && PATH="$V/bin:$PATH" bash "$B/repro_storage_migrate.sh" --newdb "repro_${label}_fs")
  (cd "$EMPTY" && "$V/bin/python" -I -X utf8 "$B/repro_failed_start.py" "$RUNNER_TEMP/repro-pg-$v" "$PGDB") \
    > "$OUT/repro-failed-start-postgres-$v.txt" 2> "$OUT/repro-failed-start-postgres-$v.stderr.txt"
  echo "exit=$?" >> "$OUT/repro-failed-start-postgres-$v.txt"
  for f in "$OUT"/repro-*-"$v".txt "$OUT"/repro-*-"$v".stderr.txt; do
    sed -i -E 's#(postgresql(\+[a-z]+)?://[^:/@ ]+):[^@ ]*@#\1:***@#g; s#'"$RUNNER_TEMP"'#<RUNNER_TEMP>#g' "$f"
  done
  echo "=== $v"; cat "$OUT/repro-storage-migrate-$v.txt" "$OUT/repro-failed-start-sqlite-$v.txt" "$OUT/repro-failed-start-postgres-$v.txt"
done
echo ok > "$RUNNER_TEMP/logs/result.txt" 2>/dev/null || true
