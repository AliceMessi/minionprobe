#!/usr/bin/env bash
# Runs on the GitHub-hosted runner (AliceMessi/linux-runner, preset "none": no
# openminion checkout, no repo test fixtures; PostgreSQL is not used).
#
#   bash linux_setup.sh <steps.sh>
#
# New venvs: openminion==0.1.13 and ==0.1.12 from PyPI, and dev @ 988ac4ec from
# GitHub. Records versions, then sources <steps.sh> from an empty directory.
# Outputs go to $RUNNER_TEMP/logs/out (uploaded as the run's artifact).
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"
mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH
DEV_SHA=988ac4ec54ae803f4ac937a6fc2fc1c04bb5f590

python -m pip install -q uv >/dev/null 2>&1
uv venv -q --python 3.12 "$RUNNER_TEMP/venv"
uv pip install -q --python "$RUNNER_TEMP/venv/bin/python" --index-url https://pypi.org/simple "openminion==0.1.13"
uv venv -q --python 3.12 "$RUNNER_TEMP/venv0112"
uv pip install -q --python "$RUNNER_TEMP/venv0112/bin/python" --index-url https://pypi.org/simple "openminion==0.1.12"
uv venv -q --python 3.12 "$RUNNER_TEMP/venvdev"
uv pip install -q --python "$RUNNER_TEMP/venvdev/bin/python" --index-url https://pypi.org/simple \
  "openminion @ git+https://github.com/openminion/openminion@$DEV_SHA"
PY="$RUNNER_TEMP/venv/bin/python"
PY0112="$RUNNER_TEMP/venv0112/bin/python"
PYDEV="$RUNNER_TEMP/venvdev/bin/python"
for v in venv venv0112 venvdev; do
  uv pip show --python "$RUNNER_TEMP/$v/bin/python" openminion > "$OUT/pip-show-$v.txt"
done
uv pip list --python "$PY" > "$OUT/pip-list.txt"
uv pip list --python "$PYDEV" > "$OUT/pip-list-dev.txt"
{ uname -a; grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release; "$PY" -c 'import sys,sqlite3;print("python", sys.version.split()[0], "sqlite", sqlite3.sqlite_version)'; echo "sqlite3 shell: $(command -v sqlite3) $(sqlite3 --version 2>/dev/null | cut -c1-40)"; } > "$OUT/os.txt"
sha256sum "$B"/*.py "$B"/*.sh > "$OUT/probe-sha256.txt"

EMPTY="$RUNNER_TEMP/empty"
mkdir -p "$EMPTY"
cd "$EMPTY"
export VERIFY_WORK="$RUNNER_TEMP"
# shellcheck source=/dev/null
source "$B/$1"
cat "$OUT/os.txt"
