#!/usr/bin/env bash
# #369 on native Windows (SQLite), Git Bash. Fresh venvs from PyPI:
#   W/venv369       openminion[acp]==0.1.12   (acp extra: the ACP session/list server)
#   W/venv369-0111  openminion[acp]==0.1.11   (seed writer, backup CLI, controls)
# Coordinated upgrade (docs/runtime-database-upgrades.md): the 0.1.11 writer
# process exits, a pre-upgrade backup is taken with the 0.1.11 CLI
# (`openminion storage backup --sqlite`), then the first 0.1.12 process
# upgrades. 0.1.11 is only ever pointed at the backup afterwards, never at the
# migrated database.
set -uo pipefail
# W and E were absolute local paths when this ran; before publishing they were replaced by
# these two variables (no other change).
W="${W:?set W to the work folder}"
E="${E:?set E to this evidence folder}"
P=$E/probes; O=$E/windows/369; C=$O/control-0.1.11; D=$W/work/369
PY=$W/venv369/Scripts/python.exe; PY0111=$W/venv369-0111/Scripts/python.exe
OM0111=$W/venv369-0111/Scripts/openminion.exe
wp() { cygpath -w "$1"; }
export VERIFY_WORK; VERIFY_WORK=$(wp "$W")
rm -rf "$D"; mkdir -p "$D" "$O" "$C" "$W/empty-369"
cd "$W/empty-369"
run() {  # run <log> <python> <probe> args...
  local log=$1 py=$2 probe=$3; shift 3
  echo "=== $(basename "$log" .log): $(basename "$(dirname "$(dirname "$py")")") -I -X utf8 probes/$probe $*" | sed "s#$W#<WORK>#g; s#$(wp "$W" | sed 's#\\#\\\\#g')#<WORK>#g" > "$log"
  "$py" -I -X utf8 "$P/$probe" "$@" >> "$log" 2>&1
  echo "exit=$?" >> "$log"
  grep -E "^(SUMMARY|FAIL )|exit=" "$log" | sed "s#^#  #"
}
"$PY" -c "import sys,sqlite3,openminion;print('openminion',openminion.__version__,'python',sys.version.split()[0],'sqlite',sqlite3.sqlite_version)" > "$O/versions.txt"
"$PY0111" -c "import sys,sqlite3,openminion;print('openminion',openminion.__version__,'python',sys.version.split()[0],'sqlite',sqlite3.sqlite_version)" >> "$O/versions.txt"
uv pip list --python "$PY" > "$O/pip-list.txt" 2>/dev/null
uv pip list --python "$PY0111" > "$C/pip-list-0.1.11.txt" 2>/dev/null

# 1. Fresh databases, filters, writes, ACP and Focus consumers. 0.1.11 first (control and SQLite baseline).
run "$C/probe_369_filters_sqlite.log" "$PY0111" probe_369_filters.py sqlite "$(wp "$D/filters-0111")" "$(wp "$C/probe_369_filters_sqlite.json")"
run "$O/probe_369_filters_sqlite.log" "$PY" probe_369_filters.py sqlite "$(wp "$D/filters")" "$(wp "$O/probe_369_filters_sqlite.json")" --baseline "$(wp "$C/probe_369_filters_sqlite.json")"

# 2. Pre-migration-15 history written by 0.1.11 (its own process, which exits).
run "$O/probe_369_seed_sqlite.log" "$PY0111" probe_369_seed.py seed sqlite "$(wp "$D/seed")" "$(wp "$O/probe_369_seed_sqlite.json")" --db "$(wp "$D/seed/base.db")"
run "$O/probe_369_seed_bad_sqlite.log" "$PY0111" probe_369_seed.py seed sqlite "$(wp "$D/seed")" "$(wp "$O/probe_369_seed_bad_sqlite.json")" --db "$(wp "$D/seed/bad.db")" --bad

# 3. Per case: copy, pre-upgrade backup with the 0.1.11 CLI, upgrade with 0.1.12, check the backup with 0.1.11.
for case in normal via14 malformed extraction; do
  src=base; seed=probe_369_seed_sqlite.json
  [[ $case == malformed ]] && { src=bad; seed=probe_369_seed_bad_sqlite.json; }
  mkdir -p "$D/$case"
  cp "$D/seed/$src.db" "$D/$case/runtime.db"
  "$OM0111" storage backup --sqlite "$(wp "$D/$case/runtime.db")" --output "$(wp "$D/$case/pre-upgrade-backup.db")" --json \
    > "$O/backup_$case.json" 2>&1
  echo "backup $case exit=$? sha256 $(sha256sum < "$D/$case/pre-upgrade-backup.db" | cut -c1-64)" | tee -a "$O/backup_$case.json"
  run "$O/probe_369_upgrade_${case}_sqlite.log" "$PY" probe_369_upgrade.py "$case" sqlite "$(wp "$D/$case")" \
    "$(wp "$O/probe_369_upgrade_${case}_sqlite.json")" --db "$(wp "$D/$case/runtime.db")" --seed "$(wp "$O/$seed")"
  run "$O/probe_369_backup_${case}_sqlite.log" "$PY0111" probe_369_seed.py verify sqlite "$(wp "$D/$case/backup-check")" \
    "$(wp "$O/probe_369_backup_${case}_sqlite.json")" --db "$(wp "$D/$case/pre-upgrade-backup.db")" --expect "$(wp "$O/$seed")"
done
(cd "$P" && sha256sum probe_369_lib.py probe_369_filters.py probe_369_seed.py probe_369_upgrade.py steps_369_windows.sh _common.py) > "$O/probe-sha256.txt"
