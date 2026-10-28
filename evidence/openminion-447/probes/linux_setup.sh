#!/usr/bin/env bash
# #447 PR evidence on the GitHub-hosted runner (AliceMessi/linux-runner, preset "none",
# PostgreSQL 16 in Docker at $OPENMINION_TEST_POSTGRES_URL). No openminion checkout is
# used: each venv installs the tree under test from git.
#
#   DEV_SHA=<sha> PR_SHA=<sha> bash linux_setup.sh
#
# Two fresh venvs (uv, Python 3.12):
#   dev  openminion[postgres] @ git+https://github.com/openminion/openminion@$DEV_SHA
#   pr   openminion[postgres] @ git+https://github.com/AliceMessi/openminion@$PR_SHA
# Each runs, with `python -I -X utf8` from an empty directory:
#   - probe_failed_start_retention.py, the #447 report's probe, unchanged;
#   - probe_447_boundaries.py, one process per case, on SQLite and on PostgreSQL
#     (a new database per case).
# Outputs go to $RUNNER_TEMP/logs/out.
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"
mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH \
  OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL OPENMINION_MEMORY_PROVIDER
export PGURL="${OPENMINION_TEST_POSTGRES_URL:-}"

python -m pip install -q uv >/dev/null 2>&1
declare -A SPEC=(
  [dev]="openminion[postgres] @ git+https://github.com/openminion/openminion@$DEV_SHA"
  [pr]="openminion[postgres] @ git+https://github.com/AliceMessi/openminion@$PR_SHA"
)
declare -A PYS
for v in dev pr; do
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv-$v"
  uv pip install -q --python "$RUNNER_TEMP/venv-$v/bin/python" --index-url https://pypi.org/simple "${SPEC[$v]}" \
    || echo "install $v failed" | tee -a "$OUT/install-errors.txt"
  PYS[$v]="$RUNNER_TEMP/venv-$v/bin/python"
  uv pip list --python "${PYS[$v]}" > "$OUT/pip-list-$v.txt" 2>&1
  "${PYS[$v]}" -c "from importlib import metadata as m; print(m.distribution('openminion').read_text('direct_url.json'))" > "$OUT/direct_url-$v.json"
done
{ echo "dev $DEV_SHA"; echo "pr $PR_SHA"; } > "$OUT/trees.txt"
{ uname -a; grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release; "${PYS[dev]}" -c 'import sys,sqlite3;print("python", sys.version.split()[0], "sqlite", sqlite3.sqlite_version)'; } > "$OUT/os.txt"
"${PYS[dev]}" - > "$OUT/postgres-version.txt" 2>&1 <<'EOF'
import os, sqlalchemy as sa
e = sa.create_engine(os.environ["PGURL"])
with e.connect() as c:
    print(c.execute(sa.text("select version()")).scalar_one())
e.dispose()
EOF
(cd "$B" && sha256sum probe_failed_start_retention.py probe_447_boundaries.py linux_setup.sh) > "$OUT/probe-sha256.txt"

admin() {  # one statement on the server's maintenance database
  "${PYS[dev]}" -I -c '
import sys, sqlalchemy as sa
url = sa.engine.make_url(sys.argv[1]).set(database="postgres")
e = sa.create_engine(url, isolation_level="AUTOCOMMIT")
with e.connect() as c:
    c.execute(sa.text(sys.argv[2]))
e.dispose()' "$PGURL" "$1"
}
dburl() {
  "${PYS[dev]}" -I -c '
import sys, sqlalchemy as sa
print(sa.engine.make_url(sys.argv[1]).set(database=sys.argv[2]).render_as_string(hide_password=False))' "$PGURL" "$1"
}

SQLITE_CASES="control memory-provider sessions-table engine-emitter storage-migration extensions ops support-authoring after-support gateway connection post-init runtime-manager bind close-fails"
PG_CASES="control memory-provider sessions-table engine-emitter storage-migration extensions ops support-authoring after-support gateway post-init runtime-manager bind"
for v in dev pr; do
  EMPTY="$RUNNER_TEMP/empty-$v"
  mkdir -p "$EMPTY"
  echo "=== $v probe_failed_start_retention"
  (cd "$EMPTY" && "${PYS[$v]}" -I -X utf8 "$B/probe_failed_start_retention.py" --label "$v" \
     --work "$RUNNER_TEMP/work-$v-retention" --out "$OUT/probe_failed_start_retention_$v.json" \
     > "$OUT/probe_failed_start_retention_$v.log" 2>&1)
  echo "exit=$?" | tee -a "$OUT/probe_failed_start_retention_$v.log"
  grep -E "^CASE" "$OUT/probe_failed_start_retention_$v.log" | cut -c1-400
  for backend in sqlite postgres; do
    cases=$SQLITE_CASES; [ $backend = postgres ] && cases=$PG_CASES
    [ $backend = postgres ] && [ -z "$PGURL" ] && continue
    for c in $cases; do
      name="$v-$backend-$c"
      args=("$c" "$RUNNER_TEMP/work-$name")
      if [ $backend = postgres ]; then
        db="om447_${v}_${c//-/_}"
        admin "CREATE DATABASE $db"
        args+=("$(dburl "$db")")
      fi
      (cd "$EMPTY" && "${PYS[$v]}" -I -X utf8 "$B/probe_447_boundaries.py" "${args[@]}" \
         > "$OUT/boundaries-$name.json" 2> "$OUT/boundaries-$name.stderr.txt")
      echo "$name exit=$?"
    done
  done
done
"${PYS[dev]}" - "$OUT" > "$OUT/boundaries-summary.txt" <<'EOF'
import json, sys
from pathlib import Path
for f in sorted(Path(sys.argv[1]).glob("boundaries-*.json")):
    try:
        r = json.loads(f.read_text().strip().splitlines()[-1])
    except Exception as exc:
        print(f.stem, "NO RESULT", exc); continue
    a, g = r["at_exception"], r["after_gc"]
    print(f"{f.stem[11:]:34} start={r['start'][:48]!r} orig={r.get('original_exception')} "
          f"closes={len(r['closes'])} max/obj={max(r['max_per_object'].values(), default=0)} "
          f"| at_exc files={a['open_files']} threads={a['start_threads']} pg={a.get('pg_other_connections')} "
          f"| after_gc files={g['open_files']} threads={g['start_threads']} pg={g.get('pg_other_connections')}")
EOF
cat "$OUT/boundaries-summary.txt"
cat "$OUT/os.txt" "$OUT/postgres-version.txt"
echo ok > "$RUNNER_TEMP/logs/result.txt" 2>/dev/null || true
