#!/usr/bin/env bash
# Runs on the GitHub-hosted runner (AliceMessi/linux-runner, preset "none": no
# openminion checkout, no repo test fixtures; PostgreSQL is not used).
#
#   bash linux_setup.sh <steps.sh>
#
# New venvs: openminion==0.1.13 from PyPI, and the PR head 2d8fd446 (dev @ 4083b663
# plus the docs change) from GitHub. Records versions, then sources <steps.sh> from
# an empty directory. Outputs go to $RUNNER_TEMP/logs/out (uploaded as the run's artifact).
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"
mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH
HEAD_SHA=2d8fd446f699c1c96345607fbcdb475c9ec85942

python -m pip install -q uv >/dev/null 2>&1
uv venv -q --python 3.12 "$RUNNER_TEMP/venv"
uv pip install -q --python "$RUNNER_TEMP/venv/bin/python" --index-url https://pypi.org/simple "openminion==0.1.13"
uv venv -q --python 3.12 "$RUNNER_TEMP/venvhead"
uv pip install -q --python "$RUNNER_TEMP/venvhead/bin/python" --index-url https://pypi.org/simple \
  "openminion @ git+https://github.com/AliceMessi/openminion@$HEAD_SHA"
PY="$RUNNER_TEMP/venv/bin/python"
PYHEAD="$RUNNER_TEMP/venvhead/bin/python"
for v in venv venvhead; do
  uv pip show --python "$RUNNER_TEMP/$v/bin/python" openminion > "$OUT/pip-show-$v.txt"
done
uv pip list --python "$PY" > "$OUT/pip-list.txt"
uv pip list --python "$PYHEAD" > "$OUT/pip-list-head.txt"
{ uname -a; grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release; "$PY" -c 'import sys,sqlite3;print("python", sys.version.split()[0], "sqlite", sqlite3.sqlite_version)'; echo "sqlite3 shell: $(command -v sqlite3) $(sqlite3 --version 2>/dev/null | cut -c1-40)"; } > "$OUT/os.txt"
sha256sum "$B"/*.py "$B"/*.sh > "$OUT/probe-sha256.txt"

EMPTY="$RUNNER_TEMP/empty"
mkdir -p "$EMPTY"
cd "$EMPTY"
export VERIFY_WORK="$RUNNER_TEMP"
# shellcheck source=/dev/null
source "$B/$1"
cat "$OUT/os.txt"
