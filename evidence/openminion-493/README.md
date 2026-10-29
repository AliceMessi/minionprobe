# OpenMinion #493: `storage migrate --module compress` finds its migrations, the fix

Validation of the PR for [openminion#493](https://github.com/openminion/openminion/issues/493): the mapping correction proposed in the report ("Possible fix") and approved there ([comment](https://github.com/openminion/openminion/issues/493#issuecomment-6485456502)). Branch `fix/493-compress-migration-discovery` on [AliceMessi/openminion](https://github.com/AliceMessi/openminion/tree/fix/493-compress-migration-discovery), three commits on `dev` @ `37fa75ff`:

| Commit | What |
|---|---|
| `1f147954` | `test(storage)`: discovery, CLI (SQLite) and PostgreSQL regressions |
| `c7945cc3` | `fix(storage)`: `"compress": ["openminion.modules.context.compress.storage"]` in `_NESTED_MODULE_STORAGE_IMPORT_PATHS` |
| `a8e317ca` | `docs(changelog)`: the entry under Unreleased. The validated head |

The report itself, its probe and its runs on dev `39da8ffb` and 0.1.14 are in [`openminion-compress-alembic-config`](../openminion-compress-alembic-config).

## What ran

One job on [AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner), [run 40814939016](https://github.com/AliceMessi/linux-runner/actions/runs/40814939016), 29 Oct 2026: a fresh clone of AliceMessi/openminion @ `a8e317cac37995993079fc0c22e3ff12eac3fe6f`, the `openminion` preset (`make dev-install` with `.deps/openminion-eval` from `dev`, then the `postgres` extra) and a PostgreSQL 16.15 server (`postgres:16` in Docker). GitHub-hosted Ubuntu 24.04.5, CPython 3.11.17, SQLite 3.45.1, SQLAlchemy 2.1.6, psycopg 3.3.6, Alembic 1.18.4, pytest 8.4.2, ruff 0.15.22, mypy 1.20.2.

The script is [`linux/runner-gates.sh`](linux/runner-gates.sh). "dev sources" means the same checkout after `git checkout 37fa75ff -- src`; the only path under `src` that differs is `src/openminion/modules/storage/migrations/alembic.py` (printed in the log). The tests are the branch's, so on dev sources this is the test commit `1f147954` plus the CHANGELOG entry.

`<focused>` = `tests/storage/test_module_alembic_onboarding.py tests/cli/test_storage_migration_commands.py tests/cli/test_storage_migrate_report_fields.py tests/tool/authoring/test_storage_foundation.py tests/storage/test_migrations_runner.py`
`<postgres>` = `tests/integration/test_storage_migrate_report_postgres.py` (in the upstream `postgres-memory-retrieval` job's list)
`<related>` = `tests/storage tests/cli tests/tool/authoring` with the test-ci markers (`not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow`)

## Results

| | head `a8e317ca` | dev `37fa75ff` sources |
|---|---|---|
| `pytest -q <focused>` | 61 passed | 4 failed, 57 passed |
| `pytest -q -m postgres <postgres>` | 2 passed | 1 failed, 1 passed |
| `pytest -q -m "<test-ci markers>" <related>` | 1 failed, 2233 passed, 3 skipped | 5 failed, 2229 passed, 3 skipped |
| the upstream `postgres-memory-retrieval` job's 8 commands, read from `.github/workflows/ci.yml` | 19, 1, 7, 6, 46, 8, 2 and 13 passed | not run |
| `make lint` | passed (mypy ratchet 4306 / 4306) | not run |
| `ruff check .` | all checks passed | not run |
| `make format-check` | 5155 files already formatted | not run |
| `make test-critical` | 489 passed, 4 skipped | not run |

The 5 new tests fail on dev sources, each for the reported reason:
- `test_module_run_migrations_bootstraps_alembic_baseline[compress]`: `RuntimeError: Alembic configuration missing. Provide alembic_ini_path or alembic_script_location.`
- `test_discovery_finds_the_compress_storage_package`: `assert None == (PosixPath('.../src/openminion/modules/context/compress/storage') / 'migrations')`
- `test_every_postgres_validated_module_has_a_script_location`: `assert ['compress'] == []` (the other 13 validated modules resolve)
- `TestStorageMigrateApply::test_compress_module_migrates_a_new_sqlite_file` and, on PostgreSQL, `test_postgres_compress_module_migrates_a_new_schema`: the entry is `{'module_id': 'compress', 'status': 'error', 'success': False, 'error': 'Alembic configuration missing. Provide alembic_ini_path or alembic_script_location.'}` instead of `{'module_id': 'compress', 'status': 'ok', 'success': True}`.

The one failure on both trees in `<related>` is `tests/storage/test_startup_validation.py::TestValidatePostgresConnectionFailure::test_unreachable_db_raises_with_redacted_url` (`Timeout (>60.0s)`), which times out with the `postgres` extra installed, also on `dev`, and is not related.

### The report's probe, unchanged

The same job then ran the report's probe, [`probes/probe_compress_alembic_config.py`](probes/probe_compress_alembic_config.py), downloaded from minionprobe `main` (sha256 `68f97f4996d6c12d7fb8dd945795d4709237a580cf323f5970e3e32006badb30`, the same as in the report; `linux/probe/probe-sha256.txt`). It ran with the checkout's `.venv` (editable install; the CLI is its `openminion` console script) and `python -I -X utf8` from an empty directory, once on the head (label `pr`) and once on dev sources (label `dev`). Each PostgreSQL case uses a new database, each SQLite case a new file.

| Case | dev `37fa75ff` sources | head `a8e317ca` |
|---|---|---|
| Discovery of the 17 installed `*/storage/migrations/env.py` packages by their `MODULE_ID` | 16; `compress` not found | 17 |
| Module ids with no script location (of 19) | `storage`, `brain`, `compress` | `storage`, `brain` (no Alembic migrations; the CLI skips both on PostgreSQL) |
| `storage migrate --backend postgres --module compress --json` on a new database | exit 1, `Alembic configuration missing ...`; no tables | exit 0, `ok`; `alembic_version` `0001_baseline`; tables `alembic_version`, `compression_failures`, `compression_runs`, `dropped_reasons`, `om_meta`; `om_meta.module_id` `compress` |
| The same in text mode | exit 1, `FAIL  compress: Alembic configuration missing ...` | exit 0, `ok  compress` |
| Control: `--module secret` on PostgreSQL and on SQLite | exit 0, `ok`, `0001_baseline` | the same |
| Each module the CLI runs on PostgreSQL, alone, on its own new database | 11 `ok`; `compress` as above; `telemetry`, `controlplane`: `syntax error at or near "AUTOINCREMENT"` (#494) | 12 `ok`; `telemetry`, `controlplane` the same (#494) |
| Full `storage migrate --backend postgres` on one new database | exit 1: 5 skipped, `session` `ok`, 12 modules `Can't locate revision identified by '0005_cron_scheduler_control'` (#446), `compress` `Alembic configuration missing ...` | exit 1: 5 skipped, `session` `ok`, 13 modules (now including `compress`) `Can't locate revision identified by '0005_cron_scheduler_control'` (#446) |
| `MigrationRunner` for compress with explicit paths, PostgreSQL and SQLite | `success=True`, `0001_baseline`; `dropped_reasons.id` an identity column on PostgreSQL | the same |
| `storage migrate --sqlite <new file> --module compress --json` | exit 1, the same message; file unchanged (no tables, `application_id` 0) | exit 0, `ok`; `0001_baseline`; `application_id` `0x4f4d000e`; the compress tables |
| compress's own `run_migrations(<new file>)` | `RuntimeError: Alembic configuration missing ...` | returns; `0001_baseline`, `application_id` `0x4f4d000e`, `om_meta.module_id` `compress` |
| `build_runtime_storage` with the PostgreSQL record backend, then the command its warning suggests | warning `Postgres module 'compress' has pending migrations. Run: openminion storage migrate --module compress`; the command exits 1 | the same warning on the new database; the command exits 0, `ok` |

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes.

- `linux/runner-gates.sh`: the job's script.
- `linux/gates-run-40814939016.txt`: the job's log (`run.log`), with `linux/head.txt`, `linux/result.txt` and `linux/run-id.txt`.
- `linux/pytest/`: the full pytest output of each step: `head-*` and `dev-*` (`focused`, `postgres`, `related`), and `pg-job-1.txt` to `pg-job-8.txt` for the upstream PostgreSQL job's commands in their `ci.yml` order.
- `linux/make-lint.txt`, `linux/make-test-critical.txt`: the full output of those targets.
- `linux/probe/`: the probe's log and JSON for `pr` (head) and `dev` (dev sources), and the hash of the downloaded probe. Passwords in URLs are masked by the probe (`***`), and its temporary directory is `<WORK>`.
- `probes/probe_compress_alembic_config.py`: the report's probe, unchanged.

Runner paths (`/home/runner/...`) are left as they are. Nothing in this folder comes from a local machine.

## How to rerun

```bash
# in a checkout of fix/493-compress-migration-discovery with `pip install -e ".[dev,postgres]"`
python -m pytest -q tests/storage/test_module_alembic_onboarding.py tests/cli/test_storage_migration_commands.py
OPENMINION_TEST_POSTGRES_URL='postgresql+psycopg://postgres:<password>@localhost:5432/postgres' \
  python -m pytest -q -m postgres tests/integration/test_storage_migrate_report_postgres.py
git checkout 37fa75ff -- src   # dev sources: the 5 new tests fail
```
