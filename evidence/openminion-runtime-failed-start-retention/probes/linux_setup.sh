#!/usr/bin/env bash
# Runs on the GitHub-hosted runner (AliceMessi/linux-runner, preset "none",
# PostgreSQL 16 in Docker at $OPENMINION_TEST_POSTGRES_URL). No openminion
# checkout is used: each venv installs the package under test.
#
#   bash linux_setup.sh
#
# Two fresh venvs (uv, Python 3.12):
#   dev     openminion[postgres] @ git+https://github.com/openminion/openminion@68d623d10317a7cea13832f8dbd51fc98a871aa5
#   0.1.13  openminion[postgres]==0.1.13 from PyPI
# Each runs probe_migrate_ledger.py and probe_failed_start_retention.py with
# `python -I -X utf8` from an empty directory. Outputs go to $RUNNER_TEMP/logs/out.
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
declare -A PYS
for v in dev 0.1.13; do
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv-$v"
  uv pip install -q --python "$RUNNER_TEMP/venv-$v/bin/python" --index-url https://pypi.org/simple "${SPEC[$v]}" \
    || echo "install $v failed" | tee -a "$OUT/install-errors.txt"
  PYS[$v]="$RUNNER_TEMP/venv-$v/bin/python"
  uv pip show --python "${PYS[$v]}" openminion sqlalchemy psycopg alembic psutil > "$OUT/pip-show-$v.txt" 2>&1
  uv pip list --python "${PYS[$v]}" > "$OUT/pip-list-$v.txt" 2>&1
done
echo "$DEV_SHA" > "$OUT/dev-commit.txt"
{ uname -a; grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release; "${PYS[dev]}" -c 'import sys,sqlite3;print("python", sys.version.split()[0], "sqlite", sqlite3.sqlite_version)'; } > "$OUT/os.txt"
"${PYS[dev]}" - > "$OUT/postgres-version.txt" 2>&1 <<'EOF'
import os, sqlalchemy as sa
e = sa.create_engine(os.environ["PGURL"])
with e.connect() as c:
    print(c.execute(sa.text("select version()")).scalar_one())
e.dispose()
EOF
(cd "$B" && sha256sum probe_migrate_ledger.py probe_failed_start_retention.py linux_setup.sh) > "$OUT/probe-sha256.txt"

for v in dev 0.1.13; do
  EMPTY="$RUNNER_TEMP/empty-$v"
  mkdir -p "$EMPTY"
  for p in probe_migrate_ledger probe_failed_start_retention; do
    echo "=== $v $p"
    (cd "$EMPTY" && PATH="$RUNNER_TEMP/venv-$v/bin:$PATH" "${PYS[$v]}" -I -X utf8 "$B/$p.py" --label "$v" \
       --work "$RUNNER_TEMP/work-$v-$p" --out "$OUT/${p}_$v.json" > "$OUT/${p}_$v.log" 2>&1)
    echo "exit=$?" | tee -a "$OUT/${p}_$v.log"
    grep -E "^CASE" "$OUT/${p}_$v.log" | cut -c1-600
  done
done
cat "$OUT/os.txt" "$OUT/postgres-version.txt"
echo ok > "$RUNNER_TEMP/logs/result.txt" 2>/dev/null || true
