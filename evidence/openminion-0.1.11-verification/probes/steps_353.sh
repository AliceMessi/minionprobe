# #353 and #365 on the Linux runner: SQLite and PostgreSQL 16.
# Sourced by linux_setup.sh from an empty directory, with PY (0.1.11), PY0110
# (0.1.10, NEED_0110=1), B (shipped probes), OUT (uploaded) and the PostgreSQL URL
# in OPENMINION_TEST_POSTGRES_URL.
WK="$RUNNER_TEMP/work"
mkdir -p "$WK"
: > "$OUT/exit-codes.txt"
run() {
  local name="$1"; shift
  "$@" 2>&1 | tee "$OUT/$name.log"
  echo "$name exit=${PIPESTATUS[0]}" | tee -a "$OUT/exit-codes.txt"
}

# Fresh 0.1.11 stores
run probe_353-sqlite   "$PY" -I "$B/probe_353.py" "$WK/353-sqlite" "$OUT/probe_353-sqlite.json" --backend sqlite
run probe_353-postgres "$PY" -I "$B/probe_353.py" "$WK/353-pg" "$OUT/probe_353-postgres.json" --backend postgres

# The issue's reproduction: control on 0.1.10, check on 0.1.11
run probe_353_issue-v0110 "$PY0110" -I "$B/probe_353_issue.py" "$WK/issue-0110" "$OUT/probe_353_issue-v0110.json" 300
run probe_353_issue-v0111 "$PY" -I "$B/probe_353_issue.py" "$WK/issue-0111" "$OUT/probe_353_issue-v0111.json" 300

# Upgrade from 0.1.10 (runtime head 11), SQLite: seed (0.1.10 writer exits), upgrade
# with 0.1.11 (backup taken first), then the backup opened with 0.1.10.
run probe_353_upgrade-sqlite-seed "$PY0110" -I "$B/probe_353_upgrade.py" "$WK/up-sqlite" "$OUT/probe_353_upgrade-sqlite-seed.json" --mode seed --backend sqlite
run probe_353_upgrade-sqlite "$PY" -I "$B/probe_353_upgrade.py" "$WK/up-sqlite" "$OUT/probe_353_upgrade-sqlite.json" --mode upgrade --backend sqlite --seed-json "$OUT/probe_353_upgrade-sqlite-seed.json"
run probe_353_upgrade-sqlite-verify-backup "$PY0110" -I "$B/probe_353_upgrade.py" "$WK/up-sqlite" "$OUT/probe_353_upgrade-sqlite-verify-backup.json" --mode verify-backup --backup "$WK/up-sqlite/backup/openminion.pre-0.1.11.db"

# Upgrade from 0.1.10, PostgreSQL: seed in its own schema (0.1.10 writer exits),
# pg_dump of that schema as the pre-upgrade backup, upgrade with 0.1.11.
SCHEMA="p353_upgrade_$(date +%s)"
run probe_353_upgrade-postgres-seed "$PY0110" -I "$B/probe_353_upgrade.py" "$WK/up-pg" "$OUT/probe_353_upgrade-postgres-seed.json" --mode seed --backend postgres --pg-schema "$SCHEMA"
docker exec pg pg_dump -U postgres -n "$SCHEMA" postgres > "$WK/353-pg-backup.sql"
echo "pg_dump -n <schema> exit=$? bytes=$(wc -c < "$WK/353-pg-backup.sql") sha256=$(sha256sum "$WK/353-pg-backup.sql" | cut -d' ' -f1)" | tee "$OUT/probe_353_upgrade-postgres-backup.txt"
run probe_353_upgrade-postgres "$PY" -I "$B/probe_353_upgrade.py" "$WK/up-pg" "$OUT/probe_353_upgrade-postgres.json" --mode upgrade --backend postgres --pg-schema "$SCHEMA" --seed-json "$OUT/probe_353_upgrade-postgres-seed.json" --backup "$WK/353-pg-backup.sql"

# #365 export (SQLite; the export command opens storage.path)
run probe_365 "$PY" -I "$B/probe_365.py" "$WK/365" "$OUT/probe_365.json"
run probe_365-v0110-control "$PY0110" -I "$B/probe_365.py" "$WK/365-0110" "$OUT/probe_365-v0110-control.json"

cat "$OUT/exit-codes.txt"
if grep -qv 'exit=0$' "$OUT/exit-codes.txt"; then exit 1; fi
