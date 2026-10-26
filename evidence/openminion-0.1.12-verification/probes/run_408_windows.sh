#!/usr/bin/env bash
# #408 (test-only) on native Windows: the twelve tests named in #408, from the
# release-tag source trees (detached worktrees, unmodified), with the
# gcguard_408 pytest plugin (cyclic GC disabled, gc.collect a counted no-op
# inside tests, open connections and tmp_path renames recorded after each test).
#
#   bash run_408_windows.sh <work_dir> <evidence_dir>
#
# <work_dir> holds src-0112 (v0.1.12), src-0111 (v0.1.11) and venv-src0112, a
# CPython 3.12.11 venv with the tag's dependencies and dev extra only
# (`uv pip install -r src-0112/pyproject.toml --extra dev`; openminion itself is
# NOT installed there, so it is imported from each tree's src/).
set -u
W="$1"; E="$2"
P="$E/probes"; O="$E/windows/408"
PY="$W/venv-src0112/Scripts/python.exe"
mkdir -p "$O/control-0.1.11"
T12=(
  "tests/test_storage_migrations.py::StorageMigrationTests::test_migrate_database_creates_expected_schema"
  "tests/test_storage_migrations.py::StorageMigrationTests::test_migrate_database_is_idempotent_on_rerun"
  "tests/storage/test_migrations_runner.py::test_runner_rolls_back_on_failed_verify"
  "tests/storage/test_migrations_runner.py::test_secret_backup_restore_round_trip_after_migrate"
  "tests/storage/test_migration_telemetry.py::test_migrate_with_verify_emits_failed_outcome_without_exception"
  "tests/storage/test_module_ops.py::TestStorageModuleOps::test_restore_from_backup"
  "tests/storage/test_module_ops.py::TestStorageModuleOps::test_restore_envelope"
  "tests/storage/test_module_ops.py::TestRollbackSafety::test_backup_then_restore_preserves_data"
  "tests/storage/test_module_ops.py::TestRollbackSafety::test_snapshot_not_silently_deleted"
  "tests/storage/test_module_ops.py::TestRollbackSafety::test_restore_is_idempotent"
  "tests/storage/test_module_ops.py::TestE2ESmoke::test_full_lifecycle_envelope_mode"
  "tests/test_api_runtime.py::APIRuntimeBootstrapTests::test_runtime_bootstraps_storage_schema"
)
PPATH="src;.;$(cygpath -w "$P")"
export PYTHONDONTWRITEBYTECODE=1
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH

run_tree() {  # run_tree <tree> <label> <out_dir> <runs> <guard:1|0>
  local tree="$1" label="$2" out="$3" runs="$4" guard="$5"
  cd "$W/$tree" || exit 1
  {
    echo "tree: $tree = $(git log -1 --format='%H %s') (git status --short: $(git status --short | wc -l) entries)"
    echo "tag: $(git describe --tags --exact-match HEAD 2>/dev/null)"
    PYTHONPATH="$PPATH" "$PY" -c "import openminion,sys,sqlite3,platform;print('openminion', openminion.__version__, 'from', openminion.__file__);print('CPython', sys.version.split()[0], 'SQLite', sqlite3.sqlite_version, platform.platform())"
  } > "$out/$label-env.txt" 2>&1
  for i in $(seq 1 "$runs"); do
    if [[ "$guard" == "1" ]]; then
      PYTHONPATH="$PPATH" GCGUARD_OUT="$(cygpath -w "$out/$label-run$i.json")" "$PY" -m pytest -q -p no:cacheprovider -p gcguard_408 -rf --tb=line "${T12[@]}" > "$out/$label-run$i.log" 2>&1
    else
      PYTHONPATH="$PPATH" "$PY" -m pytest -q -p no:cacheprovider -rf --tb=line "${T12[@]}" > "$out/$label-run$i.log" 2>&1
    fi
    echo "$label run $i exit=$? | $(grep -E '^[0-9]+ (passed|failed)' "$out/$label-run$i.log" | tail -1)" | tee -a "$out/$label-summary.txt"
  done
  git status --short > "$out/$label-git-status-after.txt"
}

(cd "$P" && sha256sum gcguard_408.py check_408_source.py run_408_windows.sh) > "$O/probe-sha256.txt"
: > "$O/v0.1.12-gcguard-summary.txt"; : > "$O/control-0.1.11/v0.1.11-gcguard-summary.txt"; : > "$O/control-0.1.11/v0.1.11-defaultgc-summary.txt"
run_tree src-0112 v0.1.12-gcguard "$O" 5 1
run_tree src-0111 v0.1.11-gcguard "$O/control-0.1.11" 2 1
run_tree src-0111 v0.1.11-defaultgc "$O/control-0.1.11" 1 0
