# #446 on the runner (sourced by linux_setup.sh from an empty directory; needs NEED_0116=1).
# The installed package under test ($PY: openminion[postgres]==0.1.17 by default) against the
# runner's postgres:16, a new database per case; the published 0.1.16 ($PY0116) builds the layouts
# earlier packages left and is the negative control. The release-by-release installed upgrade is
# steps_446_upgrade.sh, and the package, pooler and SQLite probes are steps_446_extra.sh (separate jobs,
# to keep each bundle under the dispatch limit).
set +e
run446() {  # run446 <name> <python> <probe> args...
  local name=$1 py=$2 probe=$3; shift 3
  echo "=== $name"
  "$py" -I -X utf8 "$B/$probe" "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^FAIL |^SUMMARY|^NOT RUN" "$OUT/$name.log" | cut -c1-300
}
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
"$PY" -I -c "import sqlalchemy, psycopg, alembic; print('sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'alembic', alembic.__version__)" > "$OUT/versions.txt" 2>&1
"$PY0116" -I -c "import sqlalchemy, psycopg, alembic; print('0.1.16 venv: sqlalchemy', sqlalchemy.__version__, 'psycopg', psycopg.__version__, 'alembic', alembic.__version__)" >> "$OUT/versions.txt" 2>&1
cat "$OUT/versions.txt"
HIST="$("$PY" -I -c "import openminion, pathlib; print(pathlib.Path(openminion.__file__).parent / 'modules/storage/migrations/postgres_histories.json')")"
run446 probe_446 "$PY" probe_446.py "$RUNNER_TEMP/work/p446" "$OUT/probe_446.json" --legacy-python "$PY0116"
# Negative control: the same probes on the published 0.1.16 (listed histories read from the 0.1.17 file).
run446 probe_446_0116 "$PY0116" probe_446.py "$RUNNER_TEMP/work/p446-0116" "$OUT/probe_446_0116.json" --legacy-python "$PY0116" --histories "$HIST"
docker exec pg psql -U postgres -tAc "select count(*) from pg_database where datname like 'om446%'" > "$OUT/databases-left.txt" 2>&1
echo "databases left: $(cat "$OUT/databases-left.txt"); empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
