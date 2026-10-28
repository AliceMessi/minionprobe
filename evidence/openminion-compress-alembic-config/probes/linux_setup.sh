#!/usr/bin/env bash
# Runs on AliceMessi/linux-runner with preset "none" (no openminion checkout on
# the machine) and PostgreSQL 16 in Docker at $OPENMINION_TEST_POSTGRES_URL.
#
#   bash linux_setup.sh <steps.sh>
#
# Installs openminion[postgres]==0.1.14 from PyPI into a new venv, records the
# package, OS and PostgreSQL versions, then sources <steps.sh> from an empty
# directory. Outputs go to $RUNNER_TEMP/logs/out (the run's artifact).
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"
mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH \
  OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL
export PGURL="${OPENMINION_TEST_POSTGRES_URL:-}"
python -m pip install -q uv >/dev/null 2>&1
uv venv -q --python 3.12 "$RUNNER_TEMP/venv"
uv pip install -q --python "$RUNNER_TEMP/venv/bin/python" --index-url https://pypi.org/simple "openminion[postgres]==0.1.14"
PY="$RUNNER_TEMP/venv/bin/python"
uv pip show --python "$PY" openminion > "$OUT/pip-show-0.1.14.txt"
uv pip list --python "$PY" > "$OUT/pip-list-0.1.14.txt"
{ uname -a; grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release; "$PY" -c 'import sys,sqlite3;print("python", sys.version.split()[0], "sqlite", sqlite3.sqlite_version)'; } > "$OUT/os-0.1.14.txt"
docker exec pg psql -U postgres -tAc 'select version()' > "$OUT/postgres-version-0.1.14.txt" 2>&1 || true
(cd "$B" && sha256sum ./*.py ./*.sh) > "$OUT/probe-sha256-0.1.14.txt"
EMPTY="$RUNNER_TEMP/empty"
mkdir -p "$EMPTY"
cd "$EMPTY"
# shellcheck source=/dev/null
source "$B/$1"
cat "$OUT/pip-show-0.1.14.txt" "$OUT/os-0.1.14.txt" "$OUT/postgres-version-0.1.14.txt"
