# #446 round 2: PostgreSQL layouts, read-only detection, relocation rehearsals

This folder supports the revised compatibility decision for [openminion#446](https://github.com/openminion/openminion/issues/446). The issue is that runtime storage and module Alembic migrations share one PostgreSQL schema, with colliding tables and one `alembic_version`. [`probe_446_round2.py`](probe_446_round2.py) does five things:
- it builds every layout the decision table covers, with the current code;
- it classifies each layout with a read-only prototype of the proposed detection;
- it checks that the detection changed nothing;
- it rehearses the manual relocations;
- it records the PostgreSQL behavior the proposal relies on.

The prototype is evidence for the proposal, not the fix.

## What ran

There was one runner job, [40666597470](https://github.com/AliceMessi/linux-runner/actions/runs/40666597470), on 28 Oct 2026 ([AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner), preset `none`).
- The runner is GitHub-hosted Ubuntu 24.04.5 with CPython 3.12, and its PostgreSQL 16 service ran 16.15. The probe connects as the superuser of that service and creates a new database for every case.
- The job checked out `openminion/openminion` at `39c36a1b5183ca811a9031cbcb397b3f9470a06e` (current `dev`, the 0.1.14 release merge).
- [`linux_setup.sh`](linux_setup.sh) creates two fresh `uv` venvs and runs the probe in each with `python -I -X utf8`, from an empty directory:
  - `v0114`: `openminion[postgres]==0.1.14` from PyPI;
  - `dev`: the checkout.
- Package versions: sqlalchemy 2.1.6, psycopg 3.3.6, alembic 1.18.4.
- The two builds gave the same results, line for line, apart from random database and role names: compare [`linux/probe_446_round2_dev.log`](linux/probe_446_round2_dev.log) and [`linux/probe_446_round2_v0114.log`](linux/probe_446_round2_v0114.log).
- The probe and the setup were unpacked from the job's command, not from a commit. [`linux/probe-sha256.txt`](linux/probe-sha256.txt) is the runner's hash of both files, and it matches the table at the end.

## Parts of the probe

1. **Manifest.** Each owner is built alone in its own schema, through a URL whose `options` set `search_path`. The owners are:
   - runtime storage;
   - a bare `StorageEngine`;
   - each of the 14 modules that `storage migrate` runs on PostgreSQL;
   - the memory, A2A audit, A2A state and session module stores.

   Each owner's tables and columns become its manifest.
   - Eight table names have two owners: `sessions`, `events`, `session_turn_leases`, `session_retention_holds` (runtime/session), `idempotency_keys` (runtime/A2A), `memory_records` (runtime/memory), `migrations` (runtime/memory) and `agents` (A2A/registry).
   - `compress` fails with `Alembic configuration missing`.
   - `controlplane` and `telemetry` fail with `syntax error at or near "AUTOINCREMENT"`.
2. **Layouts.** The probe builds each layout and takes a snapshot of the whole database. The snapshot covers every schema's tables with their columns and row counts, an md5 of every row, indexes, functions, triggers, `alembic_version`, `om_meta` and `migrations`. The prototype then decides for runtime storage and for memory, a2a, session, artifact and registry, inside one `READ ONLY` transaction. A second snapshot is compared with the first.
3. **Claims.** What A2A's ledger does to runtime storage's `idempotency_keys`.
4. **Relocation rehearsals.** R1, R1b and R2, as described in the proposal.
5. **Facts.** The PostgreSQL behavior the proposal relies on:
   - search_path and `options` precedence, and `PGOPTIONS`;
   - what `current_schema()` returns;
   - identifier length and quoting;
   - transactional and concurrent `CREATE SCHEMA`;
   - the privileges of a non-superuser role;
   - search_path visibility in `MigrationRunner.detect()`.

## Results

### Decisions of the detection prototype

B is `public` in every case except the last row of the first block, where it is `app`. "unchanged" means the database snapshot was identical before and after detection.

| Layout | runtime storage | memory | a2a | session | artifact | registry | unchanged |
|---|---|---|---|---|---|---|---|
| `fresh` | proceed, v1-v15 | create `public__memory` | create D | create D | create D | create D | yes |
| `runtime_only` (runtime storage, 1 session row, 1 idempotency row) | proceed, nothing pending | create D | create D | create D | create D | create D | yes |
| `memory_dedicated` (memory store, 1 record) | **reject**: would create `memory_records` (memory's) | use `public` in place | create D | create D | create D | create D | yes |
| `session_dedicated_cli_all` (`storage migrate` loop on an empty database; only `session` succeeds) | **reject**: would create `events`, `session_turn_leases`, `sessions` (session's) | create D | create D | use `public` in place | create D | create D | yes |
| `a2a_dedicated` (A2A audit store, 1 record) | **reject**: would create `idempotency_keys` (A2A's) | create D | use `public` in place | create D | create D | create D | yes |
| `runtime_then_memory` (memory migration fails and rolls back) | proceed, nothing pending | create D | create D | create D | create D | create D | yes |
| `memory_then_runtime` (runtime storage fails at v4) | **reject**: would create `memory_records` | **reject**, mixed | create D | create D | create D | create D | yes |
| `runtime_then_a2a` (both build; A2A adopts `idempotency_keys`) | proceed, nothing pending | create D | **reject**, mixed | create D | create D | create D | yes |
| `a2a_then_runtime` (runtime storage fails at v1) | **reject**: would create `idempotency_keys` | create D | use `public` in place | create D | create D | create D | yes |
| `runtime_then_session_store` (session module store fails) | proceed, nothing pending | create D | create D | create D | create D | create D | yes |
| `artifact_then_registry` (`--module` each) | proceed, v1-v15 | create D | create D | create D | **reject**, mixed | **reject**, mixed | yes |
| `runtime_then_migrate_all` | proceed, nothing pending | **reject**, mixed | create D | create D | **reject**, mixed | create D | yes |
| `both_base_and_derived_memory` | **reject**: would create `memory_records` | **reject**, both present | create D | create D | create D | create D | yes |
| `derived_steady_state` (each owner in its own schema) | proceed, nothing pending | use D | use D | use D | use D | use D | yes |
| `empty_precreated_derived` (empty `public__memory`) | proceed, nothing pending | use D (empty) | create D | create D | create D | create D | yes |
| `foreign_alembic_in_public` (`search_path=app,public`) | proceed in `app`, v1-v15 | create `app__memory` | create D | create D | create D | create D | yes |
| `R1_memory_dedicated_after_move` | proceed, nothing pending | use D | create D | create D | create D | create D | yes |
| `R1b_memory_then_runtime_after_move` | proceed, nothing pending | use D | create D | create D | create D | create D | yes |
| `R2_runtime_then_a2a_after_relocation` | proceed, nothing pending | create D | use D | create D | create D | create D | yes |

The full reasons, with tables, `om_meta`, `alembic_version` and the other owners, are on the `DECIDE` lines of the logs.

What the current code wrote, from the `STEP` lines and their `changes`:
- **Runtime storage, then the session module store**, built the way `api/queries/sessions.py` builds it:
  - it fails with `UndefinedColumn: column "session_id" referenced in foreign key constraint does not exist`;
  - before failing, it added the index `idx_sessions_status_updated` to runtime storage's `sessions`, and that index stays: the bootstrap has no transaction.
- **Runtime storage, then `storage migrate` (every module):**
  - memory reports `ok` at `0004_namespace`, and its revisions added `deleted_at`, `deleted_reason`, `event_time`, `valid_to` and `namespace_json` to runtime storage's `memory_records`;
  - the artifact tables were created;
  - registry, identity and secret report `ok` without creating their tables.
- **Memory, then runtime storage:**
  - runtime storage wrote its rows 1-3 into memory's empty `migrations` table;
  - it created the v1-v3 tables and the storage engine's tables;
  - then it failed at v4 with `DuplicateTable memory_records`.
- **A2A, then runtime storage:** runtime storage left an empty `migrations` table and the storage engine's tables, then failed at v1 with `DuplicateTable idempotency_keys`.
- **Search_path visibility:** with `search_path=app,public` and another tool's `public.alembic_version` (`othertool_0007`):
  - `MigrationRunner.detect()` reports `othertool_0007`;
  - the memory store fails with `Can't locate revision identified by 'othertool_0007'`;
  - the prototype reads `app` only.

### A2A's ledger and runtime storage's `idempotency_keys`

All of this is on the `runtime_then_a2a` layout, on a separate database.
- `idempotency_keys` has runtime storage's columns: `method, idempotency_key, request_hash, response_json, status, created_at, updated_at`. A2A's ledger is at `0005_job_idempotency_scope`.
- A2A's PostgreSQL state store (`build_a2a_state_store`) builds, then `reserve_idempotency` fails with `UndefinedColumn: column "scope" of relation "idempotency_keys" does not exist`. Nothing changed.
- An Alembic `downgrade base` with A2A's scripts removes `a2a_audit_archive`, `agents`, `audit_records` and `jobs`, and also **`idempotency_keys`, runtime storage's table, together with its row**.

### Relocation rehearsals

- **R1** (`memory_dedicated`):
  - one transaction moved 9 tables (`alembic_version`, `om_meta`, `migrations` and 6 memory tables) and the function `memory_records_search_trigger` from `public` to `public__memory`;
  - the memory store opened there at `0004_namespace`, read `rec-1` and wrote `rec-2`;
  - runtime storage then built v1-v15 in `public`.
- **R1b** (`memory_then_runtime`):
  - one transaction moved 8 tables (the memory tables, `alembic_version` and `om_meta`) and the function, leaving `migrations` (runtime rows 1-3) and the storage engine's tables in `public`;
  - runtime storage resumed and reached v15;
  - memory read `rec-1` from `public__memory`.
- **R2** (`runtime_then_a2a`):
  - A2A's 4 tables, `alembic_version` and `om_meta` were parked in `a2a_hold`;
  - `public__a2a` was created, and the A2A audit store built there at head;
  - 1 audit record was copied, and `setval` was run on `audit_records_id_seq`;
  - a new record was appended, and `query_audit` returned both records;
  - A2A's state store reserved a key in `public__a2a`;
  - runtime storage reopened in `public`, and its `idempotency_keys` was byte-identical to before.

### PostgreSQL 16.15 facts (the `FACT` lines)

- **Base schema** (`current_schema()`):
  - the default `search_path` is `"$user", public`, so `current_schema()` is `public`;
  - with a schema named after the role, it is that schema;
  - with `ALTER DATABASE ... SET search_path = app`, it is `app`;
  - with `-c search_path=missing_xyz`, it is NULL;
  - with `missing_xyz,public`, it is `public`.
- **`options` and `PGOPTIONS`:**
  - with `options=-c search_path=a -c search_path=b`, the result is `b`: the last `-c` wins;
  - an existing `-c statement_timeout=1234`, `application_name` and `connect_timeout` are kept when `-c search_path="public__memory"` is appended;
  - with `PGOPTIONS=-c statement_timeout=4321`, a URL with no `options` gets 4321ms, but a URL with any `options` gets 0, because `PGOPTIONS` is replaced;
  - the prototype helper, which starts from `PGOPTIONS`, gets 4321ms and `public__memory`.
- **Quoting:**
  - `-c search_path="AppBase__memory"` resolves to `AppBase__memory`;
  - unquoted, it resolves to no schema.
- **Identifier length:**
  - a 64-byte schema name is stored as 63 bytes;
  - a second 64-byte name with the same first 63 bytes fails with `DuplicateSchema`;
  - a 65-byte `search_path` entry resolves to the truncated 63-byte schema;
  - a name of 32 `é` is stored as 31 characters, 62 bytes.
- **Transactions:** `CREATE SCHEMA` plus `CREATE TABLE`, then rollback, leaves no schema.
- **Concurrency:** a second `CREATE SCHEMA` of the same name waits for the first transaction, then fails with `UniqueViolation ... pg_namespace_nspname_index` once the first commits.
- **Non-superuser role:**
  - `has_database_privilege(..., 'CREATE')` is false, `has_schema_privilege('public', 'USAGE')` is true, and `has_schema_privilege('public', 'CREATE')` is false;
  - `CREATE SCHEMA` is denied with `permission denied for database`;
  - runtime storage in `public` fails today with `permission denied for schema public`;
  - after `CREATE SCHEMA "public__memory" AUTHORIZATION <role>`, the role has `USAGE` and `CREATE` there, and the memory store builds and stores a record;
  - in a schema where the role has only `USAGE`, `CREATE` is false and `CREATE TABLE` is denied;
  - after `GRANT CREATE ON DATABASE`, `CREATE SCHEMA` succeeds.

## Files

- [`probe_446_round2.py`](probe_446_round2.py) and [`linux_setup.sh`](linux_setup.sh): what the runner ran, byte for byte.
- `linux/`:
  - `probe_446_round2_<build>.log`: the `VERSIONS`, `MANIFEST`, `STEP`, `STATE`, `DECIDE`, `UNCHANGED`, `CLAIM`, `RELOCATE` and `FACT` lines;
  - `probe_446_round2_<build>.json`: the manifests, Alembic chains, every snapshot (including columns, indexes and row md5s), the changes of every step, and the decisions and facts;
  - `run-40666597470.log`: the job's command output;
  - `run-id.txt`, `dev-commit.txt`, `os.txt`, `postgres-version.txt`, `pip-freeze-<build>.txt` and `probe-sha256.txt`.

  `pip-freeze-dev.txt` shows the runner's checkout path, `/home/runner/work/linux-runner/linux-runner`.
- `.gitattributes` (`* -text`): the files are published byte for byte.
- [`SHA256SUMS`](SHA256SUMS): the sha256 of every file in this folder except itself and this README.

| sha256 | file |
|---|---|
| `1419f951ebc53bc63f64aa9e4b77357069ed6b1d1fc43d3a292f4b063bfa199f` | `probe_446_round2.py` |
| `d8580d9aec2c8d3658b8947e43c560291a48764706984c2d378e07500e44cfa6` | `linux_setup.sh` |
