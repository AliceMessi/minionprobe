# #446 after #817 on the runner (sourced by linux_setup.sh from an empty directory; needs NEED_0116=1).
# $PY is the package under test (OM_SPEC: openminion[postgres]==0.1.24); this file adds the published
# 0.1.23 ($PY0123: the negative control, which predates #817, and the builder of 0.1.23 databases).
# The published 0.1.16 ($PY0116) builds the layouts earlier packages left. postgres:16, a new database
# per case. ONLY (optional) limits the sections, e.g. ONLY=engine,collision.
set +e
uv venv -q --python 3.12 "$RUNNER_TEMP/venv0123"
uv pip install -q --python "$RUNNER_TEMP/venv0123/bin/python" --index-url https://pypi.org/simple "openminion[postgres]==0.1.23"
PY0123="$RUNNER_TEMP/venv0123/bin/python"
uv pip show --python "$PY0123" openminion > "$OUT/pip-show-0.1.23.txt"
"$PY0123" -I "$B/provenance.py" "$RUNNER_TEMP/dl0123" "$OUT/provenance-linux-0.1.23.json" 0.1.23 > "$OUT/provenance-linux-0.1.23.log" 2>&1
echo "provenance 0.1.23: wheel sha256 match: $(grep -c '"wheel_sha256_matches_pypi": true' "$OUT/provenance-linux-0.1.23.json")"
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
for p in "$PY" "$PY0123" "$PY0116"; do
  "$p" -I -c "import openminion, sqlalchemy, psycopg, alembic; print('openminion', openminion.__version__, 'sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'alembic', alembic.__version__)"
done > "$OUT/versions.txt" 2>&1
cat "$OUT/versions.txt"
ONLY_ARGS=()
if [[ -n "${ONLY:-}" ]]; then ONLY_ARGS=(--only "$ONLY"); fi
run446() {  # run446 <name> <python> args...
  local name=$1 py=$2; shift 2
  echo "=== $name"
  "$py" -I -X utf8 "$B/probe_446_0124.py" "$@" "${ONLY_ARGS[@]}" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL |^SUMMARY" "$OUT/$name.log" | cut -c1-400
}
export PROBE_446_REG_ROUNDS=20
run446 probe_446_0124 "$PY" "$RUNNER_TEMP/work/p" "$OUT/probe_446_0124.json" --legacy-python "$PY0116" --prev-python "$PY0123"
unset PROBE_446_REG_ROUNDS
# Negative control: the same probe on the published 0.1.23 (before #817).
run446 probe_446_0124_on_0123 "$PY0123" "$RUNNER_TEMP/work/p0123" "$OUT/probe_446_0124_on_0123.json" --legacy-python "$PY0116" --prev-python "$PY0123"
docker exec pg psql -U postgres -tAc "select count(*) from pg_database where datname like 'om446%'" > "$OUT/databases-left.txt" 2>&1
docker exec pg psql -U postgres -tAc "select count(*) from pg_roles where rolname like 'om446%'" > "$OUT/roles-left.txt" 2>&1
echo "databases left: $(cat "$OUT/databases-left.txt"); roles left: $(cat "$OUT/roles-left.txt"); empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
