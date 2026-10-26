# #369 and #396 on the runner (sourced by linux_setup.sh from an empty directory,
# NEED_0111=1). SQLite and PostgreSQL 16 (postgres:16 container "pg").
# Coordinated upgrade (docs/runtime-database-upgrades.md): each history is
# written by a 0.1.11 process that exits; a pre-upgrade backup is taken
# (SQLite: the 0.1.11 `openminion storage backup --sqlite`; PostgreSQL:
# pg_dump of the schema); then the first 0.1.12 process upgrades. Afterwards
# 0.1.11 only opens the restored backup, never the migrated database.
set +e
# The acp extra (agent-client-protocol, for the `openminion acp` server), from PyPI, in both venvs.
uv pip install -q --python "$PY" --index-url https://pypi.org/simple "openminion[postgres,acp]==0.1.12"
uv pip install -q --python "$PY0111" --index-url https://pypi.org/simple "openminion[postgres,acp]==0.1.11"
uv pip show --python "$PY" openminion agent-client-protocol psycopg sqlalchemy > "$OUT/pip-show-acp.txt" 2>&1
uv pip show --python "$PY0111" openminion agent-client-protocol > "$OUT/pip-show-acp-0.1.11.txt" 2>&1
uv pip list --python "$PY" > "$OUT/pip-list.txt" 2>&1
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
D="$RUNNER_TEMP/work369"; O9="$OUT/369"; C9="$O9/control-0.1.11"; O6="$OUT/396"; C6="$O6/control-0.1.11"
mkdir -p "$D" "$O9" "$C9" "$O6" "$C6"
OM0111="$(dirname "$PY0111")/openminion"
DBNAME=$("$PY" -c "import os;from sqlalchemy.engine import make_url;print(make_url(os.environ['PGURL']).database)")
run() {  # run <log> <python> <probe> args...
  local log=$1 py=$2 probe=$3; shift 3
  echo "=== $(basename "$log" .log): $(basename "$(dirname "$(dirname "$py")")")/bin/python -I -X utf8 probes/$probe $*" | sed "s#$RUNNER_TEMP#<RUNNER_TEMP>#g" > "$log"
  "$py" -I -X utf8 "$B/$probe" "$@" >> "$log" 2>&1
  echo "exit=$?" >> "$log"
  echo "$(basename "$log"): $(grep -E '^SUMMARY' "$log") $(tail -1 "$log")"
}

# --- #369 fresh databases, filters, writes, concurrency, ACP and Focus (0.1.11 control first)
run "$C9/probe_369_filters_sqlite.log"   "$PY0111" probe_369_filters.py sqlite   "$D/f-0111-sqlite" "$C9/probe_369_filters_sqlite.json"
run "$O9/probe_369_filters_sqlite.log"   "$PY"     probe_369_filters.py sqlite   "$D/f-sqlite" "$O9/probe_369_filters_sqlite.json" --baseline "$C9/probe_369_filters_sqlite.json"
run "$C9/probe_369_filters_postgres.log" "$PY0111" probe_369_filters.py postgres "$D/f-0111-pg" "$C9/probe_369_filters_postgres.json"
run "$O9/probe_369_filters_postgres.log" "$PY"     probe_369_filters.py postgres "$D/f-pg" "$O9/probe_369_filters_postgres.json"

# --- #369 pre-migration-15 history: SQLite
run "$O9/probe_369_seed_sqlite.log"     "$PY0111" probe_369_seed.py seed sqlite "$D/seed" "$O9/probe_369_seed_sqlite.json" --db "$D/seed/base.db"
run "$O9/probe_369_seed_bad_sqlite.log" "$PY0111" probe_369_seed.py seed sqlite "$D/seed" "$O9/probe_369_seed_bad_sqlite.json" --db "$D/seed/bad.db" --bad
for case in normal via14 malformed extraction; do
  src=base; seed=probe_369_seed_sqlite.json
  [[ $case == malformed ]] && { src=bad; seed=probe_369_seed_bad_sqlite.json; }
  mkdir -p "$D/$case"
  cp "$D/seed/$src.db" "$D/$case/runtime.db"
  "$OM0111" storage backup --sqlite "$D/$case/runtime.db" --output "$D/$case/pre-upgrade-backup.db" --json > "$O9/backup_${case}_sqlite.json" 2>&1
  echo "backup $case exit=$? sha256 $(sha256sum < "$D/$case/pre-upgrade-backup.db" | cut -c1-64)" | tee -a "$O9/backup_${case}_sqlite.json"
  run "$O9/probe_369_upgrade_${case}_sqlite.log" "$PY" probe_369_upgrade.py "$case" sqlite "$D/$case" "$O9/probe_369_upgrade_${case}_sqlite.json" --db "$D/$case/runtime.db" --seed "$O9/$seed"
  run "$O9/probe_369_backup_${case}_sqlite.log" "$PY0111" probe_369_seed.py verify sqlite "$D/$case/backup-check" "$O9/probe_369_backup_${case}_sqlite.json" --db "$D/$case/pre-upgrade-backup.db" --expect "$O9/$seed"
done

# --- #369 pre-migration-15 history: PostgreSQL (one schema per case)
for case in normal via14 malformed extraction; do
  schema="s369_$case"; bad=""; [[ $case == malformed ]] && bad="--bad"
  run "$O9/probe_369_seed_${case}_postgres.log" "$PY0111" probe_369_seed.py seed postgres "$D/pg-$case" "$O9/probe_369_seed_${case}_postgres.json" --schema "$schema" $bad
  docker exec pg pg_dump -U postgres -d "$DBNAME" -n "$schema" --no-owner --no-privileges > "$D/pg-$case-pre-upgrade.sql" 2> "$D/pg-$case-dump.err"
  echo "pg_dump $case exit=$? bytes=$(wc -c < "$D/pg-$case-pre-upgrade.sql") sha256 $(sha256sum < "$D/pg-$case-pre-upgrade.sql" | cut -c1-64)" | tee "$O9/backup_${case}_postgres.txt"
  run "$O9/probe_369_upgrade_${case}_postgres.log" "$PY" probe_369_upgrade.py "$case" postgres "$D/pg-$case" "$O9/probe_369_upgrade_${case}_postgres.json" --schema "$schema" --seed "$O9/probe_369_seed_${case}_postgres.json"
  docker exec pg createdb -U postgres "r369_$case" >> "$O9/backup_${case}_postgres.txt" 2>&1
  docker exec -i pg psql -U postgres -d "r369_$case" -v ON_ERROR_STOP=1 -q < "$D/pg-$case-pre-upgrade.sql" >> "$O9/backup_${case}_postgres.txt" 2>&1
  echo "restore into database r369_$case exit=$?" | tee -a "$O9/backup_${case}_postgres.txt"
  run "$O9/probe_369_backup_${case}_postgres.log" "$PY0111" probe_369_seed.py verify postgres "$D/pg-$case-check" "$O9/probe_369_backup_${case}_postgres.json" --database "r369_$case" --schema "$schema" --expect "$O9/probe_369_seed_${case}_postgres.json"
done

# --- #396 run readers, SQLite and PostgreSQL (0.1.12, then 0.1.11 control)
run "$O6/probe_396.log" "$PY"     probe_396.py "$D/396" "$O6/probe_396.json"
run "$C6/probe_396.log" "$PY0111" probe_396.py "$D/396-0111" "$C6/probe_396.json"
"$PY" -c "import sys,sqlite3,openminion;print('openminion',openminion.__version__,'python',sys.version.split()[0],'sqlite',sqlite3.sqlite_version)" > "$OUT/versions.txt"
"$PY0111" -c "import sys,sqlite3,openminion;print('openminion',openminion.__version__,'python',sys.version.split()[0],'sqlite',sqlite3.sqlite_version)" >> "$OUT/versions.txt"
