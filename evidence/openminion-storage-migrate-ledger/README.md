# OpenMinion: `openminion storage migrate` and runtime storage on one PostgreSQL database

The first "Also observed" item of [openminion#437](https://github.com/openminion/openminion/issues/437), filed separately as the maintainer asked. It does not use `APIRuntime`. The runtime side is `build_runtime_storage`, called with the record backend and options that a config file with `storage.backend: postgres` resolves to. That is the call `APIRuntime` makes, so these results do not depend on #437.

## What ran

One runner job, [40502798592](https://github.com/AliceMessi/linux-runner/actions/runs/40502798592), on 27 Oct 2026. The runner is a GitHub-hosted Ubuntu 24.04.5 machine ([AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner), preset `none`) with PostgreSQL 16.15 in Docker.

- [`probes/linux_setup.sh`](probes/linux_setup.sh) creates two new venvs (`uv venv --python 3.12`):
  - `dev`: `openminion[postgres] @ git+https://github.com/openminion/openminion@68d623d10317a7cea13832f8dbd51fc98a871aa5` (the `dev` head after #432; its version string is 0.1.13);
  - `0.1.13`: `openminion[postgres]==0.1.13` from PyPI.
- Each venv runs [`probes/probe_migrate_ledger.py`](probes/probe_migrate_ledger.py) with `python -I -X utf8` from an empty directory. The same job also ran `probe_failed_start_retention.py` for [`openminion-runtime-failed-start-retention`](../openminion-runtime-failed-start-retention).
- Every case uses a new, empty database. The CLI is the installed `openminion` console script. The PostgreSQL URL has the form `postgresql+psycopg://postgres:***@localhost:5432/<new database>`.
- After each step the probe records the tables in `public`, the runtime ledger `migrations`, the Alembic ledger `alembic_version`, `om_meta`, and the columns of `sessions` and `events`.
- `openminion storage migrate --json` gives no message for a module with `"status": "error"`. The probe reads the messages in two ways, both with the CLI's own helpers (`_get_validated_module_ids`, `_get_postgres_engine`, `_make_runner`):
  - for a full run, it runs the CLI's loop in process on another new database, in the same order (`runner_messages`, `runner_messages_same_order`);
  - after a single `--module` command that failed, it runs `MigrationRunner.migrate()` for that module once more on the same database (`in_process_rerun`). The CLI's failed attempt was rolled back, so the second attempt starts from the same state.

## Results

`dev` and 0.1.13 gave the same results in every case.

| Case | Result |
|---|---|
| `build_runtime_storage` alone (control) | v15, 19 tables, `migrations` has 15 rows |
| Each of the 14 modules that `storage migrate` runs on PostgreSQL, alone, `--module <id>` (control) | 11 `ok`. `compress`: `Alembic configuration missing. Provide alembic_ini_path or alembic_script_location.` `controlplane` and `telemetry`: `(psycopg.errors.SyntaxError) syntax error at or near "AUTOINCREMENT"` |
| `storage migrate --backend postgres --postgres-url <url> --json`, every module | exit 1: 5 `skipped`, `session` `ok`, 13 `error`. None of the 13 entries has a message. The same loop in process (`runner_messages`): 12 times `Can't locate revision identified by '0005_cron_scheduler_control'` (session's head), and `compress` as above |
| Database after it | 21 tables, the same set as `--module session` alone. `alembic_version` = `0005_cron_scheduler_control`, `om_meta.module_id` = `session`, no `migrations` table |
| Then `build_runtime_storage` | `MigrationError: Migration v1 (bootstrap_core_tables) failed: (psycopg.errors.DuplicateTable) relation "sessions" already exists` |
| Database after the failed v1 | 25 tables. v1's own new tables (`messages`, `idempotency_keys`) are not there. Added: `migrations` (0 rows), `core_events`, `core_sidecar_rows`, `sidecar_ingest_log`. `sessions` and `events` keep the `session` module's columns |
| `--module session` only, then `build_runtime_storage` | CLI exit 0, `ok`; then the same `MigrationError` and the same 4 added tables |
| `build_runtime_storage` first, then `--module session` | v15; then exit 1, `error`. In process: `(psycopg.errors.UndefinedColumn) column "session_id" referenced in foreign key constraint does not exist`. No table added |
| `build_runtime_storage` first, then `storage migrate` (every module) | exit 1: `ok` for artifact, registry, identity, secret and memory; `error` for 9 modules |
| Own tables of those 5 `ok` modules afterwards | artifact: 4 of 4. registry: 0 of 3. identity: 0 of 2. secret: 0 of 1. memory: 2 of 5. The 2 are `memory_records` and `migrations`, which runtime storage had already created |
| `alembic_version` / `om_meta.module_id` afterwards | `0004_namespace` / `memory` |
| Same order, in process (messages) | session: `UndefinedColumn` as above. artifact `ok` (`None` -> `0001_baseline`). registry, identity, secret `ok` with `0001_baseline` -> `0001_baseline`. memory `ok`, `0001_baseline` -> `0004_namespace`. policy, controlplane, retrieve: `relation ... does not exist`. telemetry: `column "timestamp" does not exist`. skill, a2a, task: `Can't locate revision identified by '0004_namespace'` |
| `build_runtime_storage` again after that | builds, applies nothing (v15) |
| `--module session`, then `--module artifact` | artifact: exit 1, `{"module_id": "artifact", "status": "error", "applied": [], "success": false}`. In process: `Can't locate revision identified by '0005_cron_scheduler_control'`. No table added |
| `--module artifact`, then `--module registry` | registry: exit 0, `ok`. No table added: `agents`, `agent_methods` and `agent_status` do not exist. `alembic_version` stays `0001_baseline`. `om_meta.module_id` changes from `artifact` to `registry` |

Tables that both migration paths create, each run alone on a new database (`module_alone` and `runtime_only` in the JSON):

| Table | Runtime storage (`migrations` ledger) | Module (Alembic) |
|---|---|---|
| `sessions`, `events` | v1 | `session` (different columns) |
| `session_turn_leases` | v10 | `session` |
| `idempotency_keys` | v1 | `a2a` |
| `memory_records` | v4 | `memory` |
| `migrations` | the runtime ledger | `memory` (empty, same columns) |
| `agents` | none | `a2a` and `registry` |

The full trace of the `MigrationError` (0.1.13 and `dev` are the same; paths sanitized):

```
  File "<VENV>/lib/python3.12/site-packages/openminion/modules/storage/runtime/context.py", line 148, in build_runtime_storage
    migration_result = migrate_record_store(
  File "<VENV>/lib/python3.12/site-packages/openminion/modules/storage/runtime/migrations.py", line 769, in migrate_record_store
    raise MigrationError(
openminion.modules.storage.runtime.migrations.MigrationError: Migration v1 (bootstrap_core_tables) failed: (psycopg.errors.DuplicateTable) relation "sessions" already exists
[SQL:
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL UNIQUE,
                ...
```

## Minimal reproduction

[`minimal-repro/repro_storage_migrate.sh`](minimal-repro/repro_storage_migrate.sh) runs the commands of the report's "Minimal reproduction" one by one, with the `openminion` and `python` of the venv under test. After some steps it prints the table count, whether the registry tables exist, `alembic_version` and `om_meta.module_id`. It ran in a separate runner job, [40504243914](https://github.com/AliceMessi/linux-runner/actions/runs/40504243914), with the same two venvs ([`minimal-repro/linux_setup.sh`](minimal-repro/linux_setup.sh)). The output is the same for both builds ([`minimal-repro/linux/repro-storage-migrate-dev.txt`](minimal-repro/linux/repro-storage-migrate-dev.txt), [`...-0.1.13.txt`](minimal-repro/linux/repro-storage-migrate-0.1.13.txt)):
- `--module session`: exit 0, `ok`. Then `build_runtime_storage`: exit 1, `MigrationError: Migration v1 (bootstrap_core_tables) failed: (psycopg.errors.DuplicateTable) relation "sessions" already exists`.
- `--module session`, then `--module artifact`: artifact exit 1, `{"module_id": "artifact", "status": "error", "applied": [], "success": false}`. Afterwards: 21 tables, `alembic_version` = `0005_cron_scheduler_control`, `om_meta.module_id` = `session`.
- `--module artifact`, then `--module registry`: both exit 0, `ok`. After both: 6 tables, no registry table, `alembic_version` = `0001_baseline`, `om_meta.module_id` = `registry`.

The same job also ran the failed-start reproduction for [`openminion-runtime-failed-start-retention`](../openminion-runtime-failed-start-retention); `linux_setup.sh` and [`minimal-repro/linux/repro-sha256.txt`](minimal-repro/linux/repro-sha256.txt) cover both.

| sha256 | file |
|---|---|
| `c3f1097fcc6a9e4dca18e176150ee95b2a0b217e51793b116f9f0b1ebe6211d6` | `minimal-repro/repro_storage_migrate.sh` |
| `2b791c419c767161c758d2cccbe6c021a9c0fe3904dac5598331e14fe1a51679` | `minimal-repro/linux_setup.sh` |

## Files

- `probes/`: the probe and the runner setup, byte-identical to what the runner ran:

  | sha256 | file |
  |---|---|
  | `7f94460c55e29a265c3b007973af543c22f1f3e5be8ce0692a9ed9b1d17ea9e0` | `probe_migrate_ledger.py` |
  | `96ade72c192994d70a84b10e85abf9dcd96729c1e9a8f406292d159a7b584595` | `linux_setup.sh` |

  [`linux/probe-sha256.txt`](linux/probe-sha256.txt) is the runner's own hash of the files in the job. It also lists `probe_failed_start_retention.py`, which is in [`openminion-runtime-failed-start-retention/probes`](../openminion-runtime-failed-start-retention/probes).
- `linux/`:
  - `probe_migrate_ledger_<build>.log`: one `CASE` line per case, plus `OWN-TABLES` and `RERUN` lines;
  - `probe_migrate_ledger_<build>.json`: the CLI argv (password masked), exit codes and JSON entries, the in-process messages, the tables, ledgers, `om_meta` and columns after each step, and the trace;
  - `pip-show-*.txt` and `pip-list-*.txt` for both venvs, `dev-commit.txt`, `os.txt`, `postgres-version.txt` and `run-id.txt`.
- `.gitattributes` (`* -text`): the files are published byte for byte.
