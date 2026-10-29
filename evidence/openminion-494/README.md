# OpenMinion #494 PR: the controlplane and telemetry baselines on PostgreSQL

Evidence for the PR on branch `fix/494-postgres-baseline-ddl` of AliceMessi/openminion (head `5ef766093dfe28be8b7b9bc9287626cd51eed4fa`, four commits on `dev` @ `37fa75ff`):

| Commit | What |
|---|---|
| `b3425242` | test: `tests/integration/test_module_baselines_postgres.py` (new, added to the `postgres-memory-retrieval` job in `.github/workflows/ci.yml`) and SQLite tests in `tests/integration/test_storage_migrations_applied.py` |
| `2ef06cce` | fix: PostgreSQL DDL in both `0001_baseline` revisions, and `require_postgres_column_types` in `modules/storage/migrations/alembic.py` |
| `1d12e094` | fix: the controlplane store skips an `ADD COLUMN` whose column already exists, so it opens a database the baseline created |
| `5ef76609` | docs: CHANGELOG entry under Unreleased |

Issue: https://github.com/openminion/openminion/issues/494 (report evidence: `evidence/openminion-postgres-autoincrement-baseline`).

## What ran

All on [AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner) (GitHub-hosted Ubuntu 24.04.5), CPython 3.11.17, SQLite 3.45.1, SQLAlchemy 2.1.6, psycopg 3.3.6, Alembic 1.18.4, PostgreSQL 16.15. The `openminion` preset clones the ref and runs `make dev-install` (with `.deps/openminion-eval` from `dev`) plus the `postgres` extra.

| Run | Ref | Folder | What |
|---|---|---|---|
| [40818747439](https://github.com/AliceMessi/linux-runner/actions/runs/40818747439) | `5ef76609` (the PR head) | `linux/gates-5ef76609/` | `runner-gates.sh`: focused tests, the upstream `postgres-memory-retrieval` job's commands, related PostgreSQL tests, the related test-ci slice and the SQLite probe on the head; the focused tests with only the baseline fix (`git checkout 2ef06cce -- src`); the focused, PostgreSQL and test-ci tests on dev sources (`git checkout 37fa75ff -- src`); `ruff check .`, `make format-check`, `make lint` |
| [40819919476](https://github.com/AliceMessi/linux-runner/actions/runs/40819919476) | `5ef76609` | `linux/workflow-5ef76609/` | `make workflow-check` and `make test-critical` (no PostgreSQL) |
| [40817245678](https://github.com/AliceMessi/linux-runner/actions/runs/40817245678) | `7c1f438c` on `wip/494-explore`: a draft of the baseline DDL without the check and without the store change, plus `probe_tmp/explore_494.py` | `linux/explore-7c1f438c/` | exploration before the tests were written: how the stores and the baselines meet on SQLite and PostgreSQL |

The runner script was passed to the job as its command (base64-encoded together with the SQLite probe); `linux/gates-5ef76609/runner-gates.sh` is that script.

## Results on the head `5ef76609`

| Step | Result |
|---|---|
| `pytest -q <focused>` | 21 passed |
| same, baseline fix only (`2ef06cce` sources) | 4 failed, 17 passed: the 3 PostgreSQL controlplane store cases with `DuplicateColumn: column "next_attempt_at" of relation "cp_inbox" already exists`, and the SQLite store test with `duplicate column name: next_attempt_at` |
| same, dev sources | 14 failed, 7 passed: the 13 PostgreSQL tests with `syntax error at or near "AUTOINCREMENT"`, and the SQLite store test with `duplicate column name: next_attempt_at` |
| upstream `postgres-memory-retrieval` job, 9 commands from `ci.yml` | 19, 1, 7, 6, 46, 8, 1, 13 (the new file), 13 passed |
| `pytest -q -m postgres <postgres related>` | 15 passed, 2 deselected; the same on dev sources |
| `pytest -q -m "<test-ci markers>" <related>` | 1 failed, 1363 passed, 3 skipped; dev sources: 2 failed (the same one and the SQLite store test), 1362 passed, 3 skipped. The failure on both is `tests/storage/test_startup_validation.py::TestValidatePostgresConnectionFailure::test_unreachable_db_raises_with_redacted_url` (60 s timeout to `192.0.2.1` with the `postgres` extra; the same on dev) |
| SQLite probe, dev's migration scripts against the branch's | controlplane and telemetry: new files identical (`sqlite_master`, `alembic_version`, `application_id`, `user_version`); a file migrated with dev's scripts and then upgraded with the branch's is unchanged. `RESULT ok` |
| `ruff check .` / `make format-check` / `make lint` | all checks passed / 5156 files already formatted / passed (mypy 4306 / 4306) |
| `make workflow-check` / `make test-critical` (run 40819919476) | actionlint passed, workflow action pins clean / 489 passed, 4 skipped |

`<focused>`, `<postgres related>` and `<related>` are listed at the top of `runner-gates.sh`.

## Exploration on the draft (run 40817245678)

`probes/explore_494.py` ran on `7c1f438c`, which had the PostgreSQL DDL in both baselines but neither the column check nor the store change. One `CASE` line per step in `run-40817245678.txt`, details in `explore_494.json`:

| Case | Result |
|---|---|
| SQLite: controlplane `run_migrations`, then `SQLiteControlPlaneStore` | `OperationalError: duplicate column name: next_attempt_at` (the SQLite DDL is the same as on dev) |
| SQLite: telemetry `run_migrations`, then `SQLiteTelemetryStore` | works, timestamp `1761650000.123456` read back |
| PostgreSQL: controlplane migrate on an empty schema, then `PostgresControlPlaneStore` | migrate `success` at `0002_index_audit`; the store fails with `DuplicateColumn` on `next_attempt_at` |
| PostgreSQL: telemetry migrate, then `PostgresTelemetryStore` | works, `1761650000.123456` read back |
| PostgreSQL: the store first, then migrate (both modules) | `success` at head; the store reopened writes again (controlplane ids 3 and 4 after 1 and 2; telemetry a second event, both timestamps exact); controlplane: a second migrate `success` |
| PostgreSQL: `events` created first with `timestamp REAL` | migrate `success` at `0003_debug_query_indexes`, `timestamp` still `REAL` (adopted) |
| PostgreSQL: `cp_inbound_messages` created first with `id INTEGER PRIMARY KEY` | migrate `success` at `0002_index_audit`, `id` still without a default (adopted) |
| PostgreSQL: `events` created first with other columns | migrate fails at `CREATE INDEX ... idx_turn` with `UndefinedColumn: turn_id`, rolled back |
| PostgreSQL: migrate, `alembic downgrade base`, migrate | both modules: after the downgrade only `alembic_version` (empty) and `om_meta`; then head again |

These are why the PR adds the column check and the store change.

## How to rerun

On a checkout of the branch with `pip install -e ".[dev,postgres]"` and a PostgreSQL server in `OPENMINION_TEST_POSTGRES_URL`:

```bash
python -m pytest -q tests/integration/test_module_baselines_postgres.py tests/integration/test_storage_migrations_applied.py
git checkout 37fa75ff -- src   # dev sources: 14 failed, 7 passed
git checkout HEAD -- src
mkdir -p /tmp/dev && git archive 37fa75ff src/openminion/modules/controlplane/storage src/openminion/modules/telemetry/storage | tar -x -C /tmp/dev
(cd /tmp && python -I <evidence>/probes/probe_494_sqlite_shipped_vs_head.py /tmp/dev /tmp/out)
```

## Files

- `probes/probe_494_sqlite_shipped_vs_head.py` (sha256 `fba42737f8b8281973e012bcd2d2cab196b7179c5ad64c0bdd6290bf96b52c42`, also printed by the run) and `probes/explore_494.py` (sha256 `fd2a404f797b8156ebbb3450f9bf51c6b367445cc210dbc7465618d1e6acc130`, the file in `7c1f438c`).
- `linux/gates-5ef76609/`: `runner-gates.sh`, `gates-run-40818747439.txt` (the job's output), `probe_494_sqlite_shipped_vs_head.json`, `head.txt`, `result.txt`.
- `linux/workflow-5ef76609/`: `workflow-run-40819919476.txt` (the job's output), `head.txt`, `result.txt`.
- `linux/explore-7c1f438c/`: `run-40817245678.txt`, `explore_494.json`, `head.txt`, `result.txt`.
- `SHA256SUMS.txt`: every file in this folder. `.gitattributes` (`* -text`): the files are published byte for byte.
