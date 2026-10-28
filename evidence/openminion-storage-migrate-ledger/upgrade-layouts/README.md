# #446 upgrade impact: what PostgreSQL module stores leave in a database today

This folder supports the upgrade part of the #446 proposal. `probe_446_layouts.py` runs each case on a new, empty PostgreSQL 16 database. It prints the result of each step and the state after it: schemas, tables with row counts, `alembic_version`, `om_meta`, the rows of `migrations`, and the columns of `memory_records` and `idempotency_keys`. The cases are:
- `memory_dedicated`: `PostgresMemoryStore` on its own URL, with one record, then opened again;
- `memory_after_runtime` and `memory_before_runtime`: runtime storage (`build_runtime_storage` with `record.postgres`) and `PostgresMemoryStore` on one URL, in both orders;
- `a2a_audit_after_runtime`: runtime storage, then the A2A `PostgresAuditStore` built by `build_a2a_audit_store` on the same URL, then the audit store opened again;
- `a2a_audit_alone`: the A2A audit store first, then runtime storage on the same URL.

`linux_setup.sh` installs `openminion[postgres]==0.1.13` from PyPI and `dev` @ `988ac4ec54ae803f4ac937a6fc2fc1c04bb5f590` from git, each in a fresh `uv` venv. It runs the probe with `python -I` from an empty directory. The runner was AliceMessi/linux-runner [run 40632193872](https://github.com/AliceMessi/linux-runner/actions/runs/40632193872): Ubuntu 24.04, CPython 3.12, with a PostgreSQL 16 service.

| sha256 | file |
|---|---|
| `08aa38cdbdcb93180235bc356bdb4c10b1a29adc9e80a8c4ac1f6d362b16acad` | `probe_446_layouts.py` |
| `793a60162c012452d9fe97db284b667ffe6a0fb0e2d740e996ef81c17ccba782` | `linux_setup.sh` |
| `a3fb970d318ac7c1a5905b7ea5894f4ce030fdab51a2f1ed5ba06a07c07495f5` | `layouts-0.1.13.log` |
| `60ad06c9f087ca0fb31a471bea4f577f70f905e59a06f527e5ab1d354da35643` | `layouts-dev-988ac4ec.log` |
| `de6feaf6ba43a809d5cc7c03fb17702906c237850105093a193654f8976ebe3b` | `run-40632193872.log` |

## Results

The results are the same on 0.1.13 and `988ac4ec`.

| Case | Result | Database after it |
|---|---|---|
| Dedicated memory database | built; one record put; opened again | only memory objects: 7 memory tables (`memory_records` has 1 row), an empty `migrations`, `om_meta` with `module_id = memory`, `alembic_version` `0004_namespace`. No `core_*` tables |
| Runtime storage, then the memory store on the same URL | the memory store fails: `Alembic upgrade failed for module 'memory': (psycopg.errors.UndefinedColumn) column "search_text" of relation "memory_records" does not exist` | the runtime tables at v15; no memory ledger (the migration rolled back) |
| The memory store, then runtime storage on the same URL | runtime storage fails: `Migration v4 (add_vector_memory_tables) failed: (psycopg.errors.DuplicateTable) relation "memory_records" already exists` | the memory objects, plus the runtime tables of v1–v3. `migrations` holds runtime rows 1–3: runtime storage adopted memory's empty `migrations` table as its ledger |
| Runtime storage, then the A2A audit store on the same URL | both built; the audit store opens again | runtime tables and ledger (v15), plus A2A's `alembic_version` `0005_job_idempotency_scope`, `om_meta.module_id = a2a`, and `a2a_audit_archive`, `agents`, `audit_records`, `jobs`. `idempotency_keys` keeps runtime storage's columns: A2A adopted it |
| The A2A audit store, then runtime storage on the same URL | runtime storage fails: `Migration v1 (bootstrap_core_tables) failed: (psycopg.errors.DuplicateTable) relation "idempotency_keys" already exists` | the A2A objects, plus an empty `migrations` and the hybrid store's `core_events`, `core_sidecar_rows` and `sidecar_ingest_log` |

The runtime storage builds also log the startup check's `has pending migrations` warnings; those warning lines are in the logs.
