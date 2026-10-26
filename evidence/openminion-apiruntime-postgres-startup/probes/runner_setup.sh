#!/usr/bin/env bash
# Runs on the GitHub-hosted runner (AliceMessi/linux-runner, preset "none",
# --postgres: PostgreSQL 16 in Docker). The checkout is openminion/openminion
# at the dev commit under test; nothing from it is imported except by the "dev"
# venv, which installs that checkout.
#
#   bash runner_setup.sh
#
# Three fresh venvs: openminion[postgres]==0.1.11 and ==0.1.12 from PyPI, and
# the checked-out dev commit. Each runs probe_apiruntime_pg.py with `python -I`
# from an empty directory. Outputs go to $RUNNER_TEMP/logs/out (the run's artifact).
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
SRC="${GITHUB_WORKSPACE:-$(pwd)}"
OUT="$RUNNER_TEMP/logs/out"
mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH \
  OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL
export PGURL="${OPENMINION_TEST_POSTGRES_URL:-}"

git -C "$SRC" rev-parse HEAD > "$OUT/dev-commit.txt" 2>&1
git -C "$SRC" log -1 --format='%H %cI %s' >> "$OUT/dev-commit.txt" 2>&1
python -m pip install -q uv >/dev/null 2>&1
declare -A PYS
for v in 0.1.11 0.1.12; do
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv-$v"
  uv pip install -q --python "$RUNNER_TEMP/venv-$v/bin/python" --index-url https://pypi.org/simple "openminion[postgres]==$v"
  PYS[$v]="$RUNNER_TEMP/venv-$v/bin/python"
done
uv venv -q --python 3.12 "$RUNNER_TEMP/venv-dev"
uv pip install -q --python "$RUNNER_TEMP/venv-dev/bin/python" "openminion[postgres] @ file://$SRC"
PYS[dev]="$RUNNER_TEMP/venv-dev/bin/python"

for v in 0.1.11 0.1.12 dev; do
  uv pip show --python "${PYS[$v]}" openminion sqlalchemy psycopg > "$OUT/pip-show-$v.txt" 2>&1
  uv pip list --python "${PYS[$v]}" > "$OUT/pip-list-$v.txt" 2>&1
done
{ uname -a; grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release; "${PYS[0.1.12]}" -c 'import sys,sqlite3;print("python", sys.version.split()[0], "sqlite", sqlite3.sqlite_version)'; } > "$OUT/os.txt"
docker exec pg psql -U postgres -tAc 'select version()' > "$OUT/postgres-version.txt" 2>&1 || true
sha256sum "$B"/*.py "$B"/*.sh > "$OUT/probe-sha256.txt"

for v in 0.1.11 0.1.12 dev; do
  EMPTY="$RUNNER_TEMP/empty-$v"
  mkdir -p "$EMPTY"
  echo "=== $v"
  (cd "$EMPTY" && "${PYS[$v]}" -I -X utf8 "$B/probe_apiruntime_pg.py" --label "$v" \
     --work "$RUNNER_TEMP/work-$v" --out "$OUT/probe_$v.json" > "$OUT/probe_$v.log" 2>&1)
  echo "exit=$?" | tee -a "$OUT/probe_$v.log"
  grep -E "^CASE" "$OUT/probe_$v.log"
done
cat "$OUT/dev-commit.txt" "$OUT/os.txt" "$OUT/postgres-version.txt"
