# #446 ownership ambiguity proof: exact per-revision manifest and a read-only preflight prototype

This folder answers the review of 29 Oct 2026 on [openminion#446](https://github.com/openminion/openminion/issues/446) (comment 6485464179): table-name membership plus a column subset does not prove ownership when historical or partial tables can match more than one owner. It contains:
- a read-only prototype of the bounded preflight, [`probe/ownership_proof.py`](probe/ownership_proof.py);
- a probe, [`probe/probe_446_ambiguity.py`](probe/probe_446_ambiguity.py), that builds the owner manifest revision by revision from the code, builds representative layouts on PostgreSQL, and runs the prototype on each one;
- the runner logs and JSON.

The prototype is evidence for the proposal, not the implementation. Nothing here is a PR.

## What ran

One GitHub-hosted runner job, [40823577641](https://github.com/AliceMessi/linux-runner/actions/runs/40823577641), on 29 Oct 2026 ([AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner), preset `openminion`, `--postgres`):
- Ubuntu 24.04.5, CPython 3.11, PostgreSQL 16.15 (`postgres:16` container). The probe connects as the superuser and creates a new database for every case; the permission cases also connect as a new non-superuser role.
- The checkout is [`AliceMessi/openminion@c30c88ba`](https://github.com/AliceMessi/openminion/tree/wip/446-ambiguity-proof) (branch `wip/446-ambiguity-proof`): current `dev` `37fa75ff` plus the folder `probe_tmp/446_ambiguity/` with the three files in [`probe/`](probe/). Nothing else changed ([`linux/probe-commit-files.txt`](linux/probe-commit-files.txt)). The runner's `make dev-install` installed the checkout with the `postgres` extra into `.venv`.
- [`probe/run_on_runner.sh`](probe/run_on_runner.sh) runs two steps, each with `python -I -X utf8` from an empty directory:
  1. a fresh `uv` venv with the published `openminion[postgres]==0.1.7` runs the probe with `--release 0.1.7`: it builds the 0.1.7 manifest the same way, and two histories with 0.1.7 (`r_session_017`, `r_runtime_017`);
  2. the checkout's `.venv` runs the probe: the checkout's manifest, the 0.1.7 shapes that differ from it, then every case.
- Package versions: sqlalchemy 2.1.6, psycopg 3.3.6, alembic 1.18.4 (both venvs).
- [`linux/probe-sha256.txt`](linux/probe-sha256.txt) is the runner's hash of the three files; it matches [`SHA256SUMS.txt`](SHA256SUMS.txt).

Two preliminary jobs ran earlier commits of the same branch: [40822282757](https://github.com/AliceMessi/linux-runner/actions/runs/40822282757) (`6066d6c7`, 18 cases, no release step) and [40822934174](https://github.com/AliceMessi/linux-runner/actions/runs/40822934174) (`ee9cfdff`, 20 cases with the release step). On every case they share with the final job (18 and 20), every table class and every decision is the same. Their probe logs are in [`linux/preliminary/`](linux/preliminary/).

## The rule the prototype implements

**Manifest.** For every owner and every revision of that owner, the tables it has after that revision, each with an exact signature: column names, types (`format_type`), `NOT NULL`, and the primary key. Defaults, indexes and foreign keys are not part of the signature.
- It is generated, not written by hand: each owner is built one revision at a time in an empty schema, and the schema is read after each step (`MANIFEST` lines).
- Owners: runtime storage (v1..v15), the 11 modules whose Alembic chains run on PostgreSQL, and the memory, A2A audit, A2A state and session store bootstraps. A store bootstrap that writes no `alembic_version` (A2A state, session) is its own state; tables a store adds beyond Alembic head are accepted next to any revision of that module.
- Release shapes: a revision whose shape differs in a published release is added as a second shape of the same revision. Here, 0.1.7.

**Step 1: complete histories.** An owner has a complete history in a schema when:
- its ledger there records a revision: runtime storage when `migrations` holds exactly the rows v1..vk with runtime storage's names (k >= 1); a module when `om_meta.module_id` names it and `alembic_version` holds one row of its chain (or no row, for a store bootstrap);
- every table it has at that revision is present with exactly that revision's signature.

**Step 2: table classes.** For each table whose name is in the manifest:
- **OWNED**: exactly one complete history includes it.
- **AMBIGUOUS**: two complete histories include it; or none does and it relates to revisions of two or more owners.
- **UNKNOWN**: no complete history includes it and it relates to one owner only (a fragment of that owner's revision, a partial table, or an altered one).
- **FOREIGN**: an OpenMinion name whose columns relate to no revision of any owner.

"Relates" means: an equal signature, the same column names with other types or keys, a strict column subset, or a strict column superset, at any revision. Every owner is evaluated; the order of owners never matters, and nothing breaks a tie except a complete history.

**Step 3: decisions.** Runtime storage proceeds when the base schema has no table it creates or reads, or when its history is complete and every table with a runtime storage name is OWNED by it. A module uses the base schema in place only with a complete history there and every table of its names or related to it OWNED by it; it creates `<base>__<module>` only when the base schema holds nothing of it; it uses an existing `<base>__<module>` only when the base schema holds nothing of it and the derived schema is empty (with USAGE and CREATE for the role) or holds only its complete history. Everything else is a refusal.

**Read only.** All reads run in one `SET TRANSACTION READ ONLY` transaction with explicit schema names. The probe checks, for every run of the prototype:
- `transaction_read_only` is `on`;
- `pg_current_xact_id_if_assigned()` is NULL at the end, so the transaction never got a transaction id (no write);
- a snapshot of the whole database (every schema's tables, columns, row counts, md5 of all rows, indexes, functions, triggers, sequences, `alembic_version`, `om_meta`, `migrations`) is identical before and after.

## Results

### The manifest: tables and revisions used

Built on `dev` `37fa75ff` (`MANIFEST` and `SHARED` lines of [`linux/probe_446_ambiguity.log`](linux/probe_446_ambiguity.log); every signature in [`linux/probe_446_ambiguity.json`](linux/probe_446_ambiguity.json) under `manifest`):
- runtime storage: v1..v15 of `DEFAULT_MIGRATIONS` (`modules/storage/runtime/migrations.py:66`), applied one at a time with `migrate_record_store` on a `RecordStorePostgres`; 16 tables at v15;
- Alembic, one revision at a time with `MigrationRunner.migrate(target=<revision>)`: memory 0001_baseline..0004_namespace, a2a 0001_baseline..0005_job_idempotency_scope, session 0001_baseline..0005_cron_scheduler_control, registry 0001_baseline, artifact 0001_baseline, identity 0001_baseline, policy 0001_baseline..0002_blockchain_confirmations, retrieve 0001_baseline..0002_index, secret 0001_baseline, skill 0001_baseline..0007_workflow_observations, task 0001_baseline;
  - controlplane and telemetry fail at 0001_baseline on `AUTOINCREMENT` (#494), and compress has no Alembic location (#493); they are not in the manifest;
- store bootstraps: the memory store adds `memory_capture_bundle_receipts` and `memory_relations`; the A2A audit store equals a2a at head; the A2A state store writes `agents`, `idempotency_keys` and `jobs` with `om_meta` and no `alembic_version`; the session store adds `session_retention_holds` and `session_shares`, also with no `alembic_version`.

The eight table names that two owners create (`SHARED` and `IDENTICAL` lines):

| Table | Owner revisions (one row per distinct signature) | Same signature in two owners? |
|---|---|---|
| `sessions` | runtime v1..v6, v7..v8, v9..v15; session (every revision and the store) | no |
| `events` | runtime v1..v10, v11..v15; session | no |
| `idempotency_keys` | runtime v1..v15; a2a (every revision and both stores) | no |
| `memory_records` | runtime v4..v15; memory 0001, 0002, 0003, 0004 | no |
| `agents` | a2a; registry 0001 | no |
| `session_turn_leases` | runtime v10..v15 (`migrations.py:372`); session 0001..0005 and store (`session/storage/schema.py:181`) | **yes** |
| `session_retention_holds` | runtime v11..v15 (`migrations.py:402`); session store (`session/retention.py:266`) | **yes** |
| `migrations` | runtime v1..v15 (`migrations.py:616`); memory 0001..0004 (`memory/storage/migrations/versions/0001_baseline.py:232`) | **yes** |

So three names cannot be attributed by columns, types, `NOT NULL` or primary key at all; only a ledger can attribute them.

**Release shapes** (`RELEASE` lines): the published 0.1.7 has the same shape as `dev` for every owner and revision it has (runtime v1..v11 and every module) except session. Its session `0001_baseline`..`0004_task_plan_step_start` and its session store have no `cron_scheduler_control`. On `dev`, `0001_baseline` creates it, because the baseline takes its DDL from `session/storage/schema.py` (`CRON_SCHEMA`, `0001_baseline.py:29`), which gained the table in 0.1.8 (`aa1ddf7e`). By git, between 0.1.0 and `dev` no existing Alembic revision file changed (revisions were only added, such as session `0005` in 0.1.8), `session/storage/schema.py` changed only in 0.1.8, and runtime storage only appended versions (v12..v15 after 0.1.10). So a revision's shape can change without its file changing, when the file imports its DDL; session is the one case found. Releases before 0.1.7 were not built, so their store bootstraps are not checked.

### Cases

B is `public`, D is `public__<module>`. Decisions: runtime, memory, a2a, session, registry, artifact; "create D" means the module would create `public__<module>`. "Current code on a copy" runs today's code for each refused owner on a `TEMPLATE` copy of the same database (`TODAY` lines).

| Case | Layout | Classes (beyond plain OWNED) | Prototype decision | Current code on a copy |
|---|---|---|---|---|
| `a_runtime` | runtime storage v1..v15, one session and one idempotency row | `migrations`, `session_turn_leases`, `session_retention_holds` OWNED(runtime): only runtime's history includes them | runtime proceeds (complete, nothing pending); 5 modules create D | - |
| `a_memory` | memory store, one record | `migrations` OWNED(memory) | memory in place; runtime refused; others create D | runtime: commits v1..v3, then `DuplicateTable memory_records` at v4 |
| `a_a2a` | A2A audit store, one record | - | a2a in place; runtime refused | runtime: creates `migrations` and the storage-engine tables, then `DuplicateTable idempotency_keys` at v1 |
| `a_session` | `--module session` | `session_turn_leases` OWNED(session) | session in place; runtime refused | runtime: same, `DuplicateTable sessions` at v1 |
| `a_registry` | `--module registry`, one agent | - | registry in place; runtime proceeds v1..v15 next to it | - |
| `a_memory_0002` | memory migrated to `0002_audit` only, one record | `migrations` OWNED(memory) | memory in place, pending 0003..0004; runtime refused | runtime: v1..v3, then fails at v4 |
| `b_shared` | `session_turn_leases` and `session_retention_holds` alone with their shared column sets, one row each, no ledger | both AMBIGUOUS (runtime v10/v11..v15 and session) | runtime refused; session refused | runtime: commits v1..v9, then `DuplicateTable` at v10; session: migrates to 0005 and adopts `session_turn_leases` |
| `b_rt_a2a` | runtime storage with rows, then the A2A audit store with a record (round-2 row 5) | `idempotency_keys` OWNED(runtime): runtime storage's columns, in its complete history; A2A's other tables UNKNOWN (A2A's ledger records `0005`, but its `idempotency_keys` is not there) | runtime proceeds (complete, nothing pending); a2a refused | a2a: opens and appends a record, next to runtime storage's `idempotency_keys` |
| `b_rt_sstore` | runtime storage with rows, then the session module store as `api/queries/sessions.py` builds it (fails, leaves `idx_sessions_status_updated` on runtime storage's `sessions`) | runtime's tables OWNED(runtime); the extra index is not compared | runtime proceeds; session creates D | - |
| `b_mem_rt` | memory store, then runtime storage (fails at v4) | `migrations` AMBIGUOUS: in two complete histories, runtime@v3 and memory@0004 | runtime refused; memory refused | runtime: fails at v4 again; memory: opens and writes a record |
| `b_lone` | A2A audit store, then runtime storage (fails at v1): an empty `migrations` is left | `migrations` AMBIGUOUS: empty, no complete history, equal to runtime's and memory's | runtime refused; memory refused; a2a in place | runtime: fails at v1; memory: `Can't locate revision '0005_job_idempotency_scope'` |
| `c_sessions` | `sessions` with 4 of runtime storage's v1 columns, one row | `sessions` UNKNOWN (column subset of runtime) | runtime refused; session creates D | runtime: creates `migrations` and the storage-engine tables, then fails at v1 |
| `c_agents` | `agents(agent_id, updated_at)`, one row | `agents` AMBIGUOUS (column subset of a2a's and of registry's) | a2a refused; registry refused; runtime proceeds | a2a: builds and appends next to the partial `agents`; registry: `UndefinedColumn source` |
| `c_ahead` | memory tables at `0002_audit`, ledger rows from `0004_namespace` | memory tables UNKNOWN (0002 shape under a 0004 ledger); `migrations` AMBIGUOUS | memory refused; runtime refused | memory: creates 2 store tables, then `UndefinedColumn event_time`; runtime: v1..v3, then fails at v4 |
| `c_altered` | runtime storage, then `--module artifact`, then `--module memory` (memory skips its baseline and adds 5 columns to runtime storage's `memory_records`) | runtime storage's tables UNKNOWN (its ledger says v15, `memory_records` has 5 extra columns); `migrations`, `session_turn_leases`, `session_retention_holds` AMBIGUOUS; artifact's tables UNKNOWN (`om_meta` says memory) | runtime refused although nothing is pending; memory, session, artifact refused | runtime: starts, nothing pending; memory: creates 2 tables, then `UndefinedColumn type`; session and artifact: `Can't locate revision '0004_namespace'` |
| `d_foreign` | another application's `sessions`, `idempotency_keys` and `customers` | `sessions`, `idempotency_keys` FOREIGN; `customers` not read | runtime refused; the modules create D | runtime: creates `migrations` and the storage-engine tables, then fails at v1 |
| `e_ok` | empty `public__memory` created with `AUTHORIZATION` for a role without CREATE on the database or on `public`; prototype runs as that role | - | memory uses D (empty, owned by the role, USAGE and CREATE); runtime refused (no CREATE on `public`); other modules refused (no CREATE on the database), each with the DBA statement | not run |
| `e_ok` after | the memory store built in `public__memory` as the role, one record | - | memory uses D (complete history 0004) | not run |
| `e_nogrant` | empty `public__memory` owned by the superuser, no grant | - | memory refused, message gives `GRANT USAGE, CREATE ON SCHEMA "public__memory"` or `CREATE SCHEMA ... AUTHORIZATION` | not run |
| `e_both` | memory history in `public` and an empty `public__memory` | - | memory refused: two locations | not run |
| `e_foreign` | `public__memory` holding another application's table | - | memory refused: D holds `notes` | not run |
| `r_runtime_017` | runtime storage written by the published 0.1.7 (v1..v11), with rows | 0.1.7 shapes equal `dev`'s | runtime proceeds, pending v12..v15 (with and without the 0.1.7 shapes) | not run |
| `r_session_017` | `--module session` written by the published 0.1.7 (at `0004_task_plan_step_start`), one row | without 0.1.7 shapes: no complete history (`cron_scheduler_control` missing), session tables UNKNOWN; with them: complete | without 0.1.7 shapes: session refused; with them: session in place, pending 0005 | not run |

The full classes, details and refusal messages are on the `TABLE` and `DECIDE` lines; the ledgers and complete histories on the `LEDGER` lines.

**Nothing was written.** The prototype ran 25 times (22 cases, `e_ok` after the store, and the two release cases once more with the `dev`-only manifest). Every `UNCHANGED` line reads: database identical before/after `True`, `transaction_read_only=on`, `transaction id assigned: None`. The `ledgers` part of each line shows `alembic_version`, `om_meta` and `migrations`, which were part of the compared snapshot.

## Rerun

On a runner with the openminion preset and PostgreSQL (`OPENMINION_TEST_POSTGRES_URL` set to a superuser URL), from a checkout of `wip/446-ambiguity-proof`:

```bash
bash probe_tmp/446_ambiguity/run_on_runner.sh
```

With [linux-runner](https://github.com/AliceMessi/linux-runner): `python -I run.py --repo AliceMessi/openminion --ref c30c88ba6e912ca5e7fcd5808cb1106128b6df12 --preset openminion --postgres -- "bash probe_tmp/446_ambiguity/run_on_runner.sh"`. Without the release step: `.venv/bin/python -I -X utf8 probe_tmp/446_ambiguity/probe_446_ambiguity.py out.json`.

## Not covered

- SQLite: not run. The preflight is PostgreSQL only; module stores keep their own SQLite files (`docs/runtime-database-upgrades.md`, "PostgreSQL runtime storage").
- Defaults, indexes and foreign keys are not compared. The one known case, the session store's `idx_sessions_status_updated` left on runtime storage's `sessions` (`b_rt_sstore`), is a non-unique index.
- Release shapes were built for 0.1.7 only.
- Relocation and rollback were rehearsed in round 2 ([openminion-446-layouts](https://github.com/AliceMessi/minionprobe/tree/main/evidence/openminion-446-layouts)), not here.
- Concurrent writers during the preflight are not covered; the preflight takes no locks.

## Files

- [`probe/`](probe/): `ownership_proof.py` (the prototype), `probe_446_ambiguity.py`, `run_on_runner.sh`, as committed on `wip/446-ambiguity-proof`.
- [`linux/`](linux/):
  - `probe_446_ambiguity.log` and `.json`: the checkout run (`VERSIONS`, `MANIFEST`, `SHARED`, `IDENTICAL`, `RELEASE`, `CASE`, `STEP`, `STATE`, `LEDGER`, `TABLE`, `DECIDE`, `UNCHANGED`, `TODAY` lines; the JSON has every signature, snapshot summary, class and decision);
  - `release_0.1.7.log` and `.json`: the 0.1.7 manifest and histories;
  - `run-40823577641.log` (the job's command output), `result.txt`, `head.txt`, `run-id.txt`, `head-commit.txt`, `dev-commit.txt`, `probe-commit-files.txt`, `probe-sha256.txt`, `postgres-version.txt`, `os.txt`, `pip-freeze-checkout.txt`, `pip-freeze-0.1.7.txt`;
  - `preliminary/<run id>/`: the preliminary jobs' probe logs and run ids.
- `.gitattributes` (`* -text`): files are published byte for byte.
- [`SHA256SUMS.txt`](SHA256SUMS.txt): sha256 of every file except itself.
