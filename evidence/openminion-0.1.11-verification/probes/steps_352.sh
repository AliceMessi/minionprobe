# #352 on the Linux runner: SQLite and PostgreSQL 16. Sourced by linux_setup.sh
# (run with NEED_0110=1) from an empty directory; PY = openminion 0.1.11,
# PY0110 = openminion 0.1.10, both installed from PyPI.
D="$RUNNER_TEMP/work352"
mkdir -p "$D"
run() {  # run <name> <python> <probe> args...
  local name=$1 py=$2 probe=$3
  shift 3
  echo "=== $name: $( [[ $py == "$PY0110" ]] && echo 0.1.10 || echo 0.1.11 ) python -I $probe $*" | sed "s#$RUNNER_TEMP#<RUNNER_TEMP>#g" | tee "$OUT/$name.log"
  mkdir -p "$EMPTY/$name"
  (cd "$EMPTY/$name" && "$py" -I "$B/$probe" "$@" 2>&1 | tee -a "$OUT/$name.log"; echo "exit=${PIPESTATUS[0]} cwd_files=$(ls -A | wc -l)" | tee -a "$OUT/$name.log")
}
pgdump() {  # pgdump <schema> <file>: the pre-upgrade backup of one schema
  docker exec pg pg_dump -U postgres -n "$1" postgres > "$2"
  echo "backup $1: pg_dump -n $1 -> $(wc -c < "$2") bytes, sha256 $(sha256sum "$2" | cut -c1-64), COPY blocks $(grep -c '^COPY ' "$2")" | tee -a "$OUT/backups.log"
}

for BK in sqlite postgres; do
  if [[ $BK == sqlite ]]; then
    T_SEED12="sqlite:$D/seed12.db"; T_DIRECT="sqlite:$D/direct.db"; T_FRESH="sqlite:$D/fresh.db"
    T_CONTRACT="sqlite:$D/contract.db"; T_DIFF0110="sqlite:$D/diff0110.db"; T_DIFF0111="sqlite:$D/diff0111.db"
  else
    T_SEED12="postgres:s352_seed12"; T_DIRECT="postgres:s352_direct"; T_FRESH="postgres:s352_fresh"
    T_CONTRACT="postgres:s352_contract"; T_DIFF0110="postgres:s352_diff0110"; T_DIFF0111="postgres:s352_diff0111"
  fi
  P="probe_352"; X="${BK}"
  # 0.1.10 writes the history (head 11) twice: one copy upgraded via 12 then 13, one directly.
  run ${P}_history_0110_${X} "$PY0110" probe_352_history.py "$D/h0110_$X" "$OUT/${P}_history_0110_${X}.json" "$T_SEED12"
  run ${P}_history_0110_direct_${X} "$PY0110" probe_352_history.py "$D/h0110d_$X" "$OUT/${P}_history_0110_direct_${X}.json" "$T_DIRECT"
  if [[ $BK == postgres ]]; then
    pgdump s352_seed12 "$D/backup_s352_seed12.sql"
    pgdump s352_direct "$D/backup_s352_direct.sql"
  fi
  run ${P}_check_upgrade12_${X} "$PY" probe_352_check.py "$D/up12_$X" "$OUT/${P}_check_upgrade12_${X}.json" "$T_SEED12" --mode upgrade --step12 --seed "$OUT/${P}_history_0110_${X}.json"
  run ${P}_check_upgrade_direct_${X} "$PY" probe_352_check.py "$D/direct_$X" "$OUT/${P}_check_upgrade_direct_${X}.json" "$T_DIRECT" --mode upgrade --seed "$OUT/${P}_history_0110_direct_${X}.json"
  # Roll-back material: the pre-upgrade backup, read back by 0.1.10.
  if [[ $BK == sqlite ]]; then
    run ${P}_backup_0110_${X} "$PY0110" probe_352_history.py "$D/bk_$X" "$OUT/${P}_backup_0110_${X}.json" "sqlite:$D/up12_$X/backup-pre-upgrade.db" --verify-only
  else
    docker exec pg createdb -U postgres restore352
    docker exec -i pg psql -q -U postgres -d restore352 < "$D/backup_s352_seed12.sql" > "$D/restore.log" 2>&1
    echo "restore of backup_s352_seed12.sql into database restore352: psql exit $?" | tee -a "$OUT/backups.log"
    OPENMINION_TEST_POSTGRES_URL="${PGURL%/*}/restore352" run ${P}_backup_0110_${X} "$PY0110" probe_352_history.py "$D/bk_$X" "$OUT/${P}_backup_0110_${X}.json" "postgres:s352_seed12" --verify-only
  fi
  run ${P}_compare_backup_${X} "$PY" probe_352_compare.py digest "$OUT/${P}_compare_backup_${X}.json" "$OUT/${P}_history_0110_${X}.json" "$OUT/${P}_backup_0110_${X}.json"
  # The same history written by 0.1.11 into a fresh database.
  run ${P}_history_0111_${X} "$PY" probe_352_history.py "$D/h0111_$X" "$OUT/${P}_history_0111_${X}.json" "$T_FRESH"
  run ${P}_check_fresh_${X} "$PY" probe_352_check.py "$D/fresh_$X" "$OUT/${P}_check_fresh_${X}.json" "$T_FRESH" --mode fresh --seed "$OUT/${P}_history_0111_${X}.json"
  run ${P}_compare_readers_${X} "$PY" probe_352_compare.py readers "$OUT/${P}_compare_readers_${X}.json" "$OUT/${P}_check_fresh_${X}.json" "$OUT/${P}_check_upgrade12_${X}.json" "$OUT/${P}_check_upgrade_direct_${X}.json"
  run ${P}_contract_${X} "$PY" probe_352_contract.py "$D/contract_$X" "$OUT/${P}_contract_${X}.json" "$T_CONTRACT"
  run ${P}_diff_0110_${X} "$PY0110" probe_352_diff.py "$D/diff0110_$X" "$OUT/${P}_diff_0110_${X}.json" "$T_DIFF0110" 1,2,3 40
  run ${P}_diff_0111_${X} "$PY" probe_352_diff.py "$D/diff0111_$X" "$OUT/${P}_diff_0111_${X}.json" "$T_DIFF0111" 1,2,3 40
  run ${P}_compare_diff_${X} "$PY" probe_352_compare.py diff "$OUT/${P}_compare_diff_${X}.json" "$OUT/${P}_diff_0110_${X}.json" "$OUT/${P}_diff_0111_${X}.json"
done
echo "=== totals"
grep -h -E '^(PASS|FAIL) ' "$OUT"/probe_352_*.log | cut -c1-4 | sort | uniq -c
! grep -q -h '^FAIL ' "$OUT"/probe_352_*.log
