#!/usr/bin/env bash
# Runs on the GitHub-hosted runner (AliceMessi/linux-runner, preset "none", so no
# openminion checkout and no repo test fixtures; PostgreSQL 16 in Docker).
#
#   bash linux_setup.sh <steps.sh>
#
# Installs openminion[postgres]==0.1.13 (and 0.1.12 when NEED_0112=1) from PyPI
# into new venvs, records provenance and database versions, then sources
# <steps.sh> from an empty directory. Outputs go to $RUNNER_TEMP/logs/out
# (uploaded as the run's artifact).
#
# Variables the steps file can use: PY (0.1.13), PY0112 (0.1.12, when
# NEED_0112=1), B (this bundle), OUT (outputs), PGURL (PostgreSQL URL),
# EMPTY (the empty working directory), RUNNER_TEMP.
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"
mkdir -p "$OUT"
# The runner workflow exports these for its presets; the probes use explicit roots.
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH
export PGURL="${OPENMINION_TEST_POSTGRES_URL:-}"

python -m pip install -q uv >/dev/null 2>&1
uv venv -q --python 3.12 "$RUNNER_TEMP/venv"
uv pip install -q --python "$RUNNER_TEMP/venv/bin/python" --index-url https://pypi.org/simple "${OM_SPEC:-openminion[postgres]==0.1.13}"
PY="$RUNNER_TEMP/venv/bin/python"
if [[ "${NEED_0112:-0}" == "1" ]]; then
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv0112"
  uv pip install -q --python "$RUNNER_TEMP/venv0112/bin/python" --index-url https://pypi.org/simple "openminion[postgres]==0.1.12"
  PY0112="$RUNNER_TEMP/venv0112/bin/python"
  uv pip show --python "$PY0112" openminion > "$OUT/pip-show-0.1.12.txt"
fi
uv pip show --python "$PY" openminion > "$OUT/pip-show.txt"
uv pip list --python "$PY" > "$OUT/pip-list.txt"
{ uname -a; grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release; "$PY" -c 'import sys,sqlite3;print("python", sys.version.split()[0], "sqlite", sqlite3.sqlite_version)'; } > "$OUT/os.txt"
docker exec pg psql -U postgres -tAc 'select version()' > "$OUT/postgres-version.txt" 2>&1 || true
sha256sum "$B"/*.py "$B"/*.sh > "$OUT/probe-sha256.txt"

EMPTY="$RUNNER_TEMP/empty"
mkdir -p "$EMPTY"
cd "$EMPTY"
export VERIFY_WORK="$RUNNER_TEMP"
"$PY" -I "$B/provenance.py" "$RUNNER_TEMP/dl" "$OUT/provenance-linux.json" > "$OUT/provenance-linux.log" 2>&1
echo "provenance: $(grep -c '"wheel_sha256_matches_pypi": true' "$OUT/provenance-linux.json") match"

# shellcheck source=/dev/null
source "$B/$1"
# Provenance only: the package, Python, OS and PostgreSQL versions on the runner.
cat "$OUT/pip-show.txt" "$OUT/os.txt" "$OUT/postgres-version.txt"
"$PY" -I -c "import openminion,sys;print(openminion.__file__.replace(sys.prefix,'<VENV>'), openminion.__version__)"
