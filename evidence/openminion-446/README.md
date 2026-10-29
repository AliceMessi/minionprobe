# #446: PostgreSQL schema per owner, the PR's evidence

Evidence for the PR that implements the bounded rule approved on [openminion#446](https://github.com/openminion/openminion/issues/446) ([comment 6494431887](https://github.com/openminion/openminion/issues/446#issuecomment-6494431887)). Branch `fix/446-module-migration-ownership` on [AliceMessi/openminion](https://github.com/AliceMessi/openminion), head `f04b4eae` on `dev` `48332187` (#521 merged): `test(storage): ...` `012b6ae8`, `fix(storage): ...` `ede74ca3`, `docs(storage): ...` `f04b4eae`. This head answers two independent reviews (of `5fc5f9e5`, then of `0c1b8ebd`); those heads' runs are kept in `superseded-5fc5f9e5/` and `superseded-0c1b8ebd/` and no longer describe the PR.

Everything ran on GitHub-hosted runners ([AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner)): Ubuntu 24.04.5, CPython 3.11.17, PostgreSQL 16.15 (`postgres:16`), SQLAlchemy 2.1.6, psycopg 3.3.6, Alembic 1.18.4. Every probe step runs with `python -I -X utf8` from an empty directory. Published packages are installed from PyPI into their own `uv` venvs (`openminion[postgres]==X`; dependencies resolved on the day of the run). The branch build is the checkout installed non-editable into its own venv.

## 1. What every published package creates (the listed histories)

Run [40875868078](https://github.com/AliceMessi/linux-runner/actions/runs/40875868078) (`linux/measure-40875868078/`), probe commit `224e34b7` = `dev` `48332187` plus `probe_tmp/446_histories/` only.

[`probes/probe_446_histories.py`](probes/probe_446_histories.py) ran with each of the 45 releases on PyPI (0.0.1 to 0.1.15) and with `dev` `48332187`. On a new database per case it builds runtime storage v1..vk one version at a time and with one `build_runtime_storage` start; every module with an Alembic chain, one revision at a time; every PostgreSQL module store alone, after its module's Alembic head, and followed by it. After each step it reads every table's exact signature (column names, `format_type`, `NOT NULL`, primary key) and the ledgers. [`probes/make_histories.py`](probes/make_histories.py) turns every successful step into one history (owner, ledger, exact tables), listed once with every package that built it: [`data/histories-measured.json`](data/histories-measured.json), 96 histories over 127 signatures. Before #521, `controlplane` and `telemetry` fail on `AUTOINCREMENT` in every release; the A2A factories do not exist before 0.0.29.

## 2. Which historical layouts are listed

Runs [40881330002](https://github.com/AliceMessi/linux-runner/actions/runs/40881330002), [40881330356](https://github.com/AliceMessi/linux-runner/actions/runs/40881330356), [40881330724](https://github.com/AliceMessi/linux-runner/actions/runs/40881330724) (`linux/upgrade-verify-*`): the installed upgrade of section 3, run on the pre-review code (`731a0513`, probe commit `641fd41c`) with the unfiltered 96 histories. The listed histories have not changed since; the head's own upgrade runs in section 3 re-confirm every listed and refused layout. [`probes/filter_histories.py`](probes/filter_histories.py) keeps a history when this code builds it, or when it is the layout a release leaves and its installed upgrade passed for every release that left it ([`data/filter-output.txt`](data/filter-output.txt)): 87 histories, 18 of them historical with `upgraded_from`; 9 dropped. The result is exactly the PR's `postgres_histories.json` ([`data/postgres_histories.json`](data/postgres_histories.json)), unchanged by the review.

## 3. Installed upgrade and restore on the PR head

Runs [40914601043](https://github.com/AliceMessi/linux-runner/actions/runs/40914601043) (0.0.1..0.0.15), [40914607068](https://github.com/AliceMessi/linux-runner/actions/runs/40914607068) (0.0.16..0.0.30), [40914613614](https://github.com/AliceMessi/linux-runner/actions/runs/40914613614) (0.1.0..0.1.15) (`linux/upgrade-final-*`). The probe commit is `4fed8bab` = the PR head `f04b4eae` plus `probe_tmp/446/` only; the branch build is the PR head installed from the checkout.

[`probes/probe_446_upgrade.py`](probes/probe_446_upgrade.py) `drive` runs, for every release and every owner it can build, on a new database:
1. **build** with the release, in `public`, the way an installation of that release does: a runtime storage start; `MigrationRunner.migrate("head")` as `storage migrate` runs it; each module store; `storage migrate` then the store (`after:`), and the store then `storage migrate` (`before:`); and one mixed case, `runtime+session_store`: runtime storage, then the session store on the same URL as the API's session trace query builds it. It then adds one row to every table it can (generic values for `NOT NULL` columns without a default);
2. snapshot and `pg_dump`;
3. **upgrade** with the branch build through its production entry points: `build_runtime_storage`, the installed `openminion storage migrate --backend postgres` console script, `resolve_memory_backend`, `build_a2a_audit_store`, the store factories (`storage migrate` before the store, as the docs say);
4. checks: no `<base>__<module>` schema was created (for the mixed case: only `public__session`, by `storage migrate --module session` and the session store), the owner's layout afterwards is one of the branch's histories built by this code, and every row that existed before is still there over the columns it had (rows the upgrade appends, such as runtime storage's `migrations` rows, are reported apart);
5. **restore**: the dump is restored into a new database and the release opens it again; its layout and rows must equal the pre-upgrade ones.

The verdict per case is PASS, REFUSED (the branch refused with `Nothing was written` and the database is unchanged), or FAIL ([`data/upgrade-summary.txt`](data/upgrade-summary.txt), from [`probes/summarize_upgrade.py`](probes/summarize_upgrade.py)):

| Releases | Cases built per release | PASS | REFUSED | FAIL |
| --- | --- | --- | --- | --- |
| 0.1.0..0.1.15 | 42 | all | 0 | 0 |
| 0.0.29..0.0.30 | 42 | all | 0 | 0 |
| 0.0.14..0.0.28 | 39 | all | 0 | 0 |
| 0.0.11..0.0.13 | 40 | 39 | 1 (policy store) | 0 |
| 0.0.8..0.0.10 | 41 | 39 | 2 (policy store, session store) | 0 |
| 0.0.4..0.0.7 | 41 | 38 | 3 (policy, session and telemetry stores) | 0 |
| 0.0.1..0.0.3 | 41 | 35 | 6 (the three stores; session migrated alone, before and after its store) | 0 |

Total: 1793 PASS, 39 REFUSED, 0 FAIL.
- PASS: in place, every pre-upgrade row kept over its columns, the result is one of this code's histories, and the restored dump opens with the release with the same layout and rows.
- REFUSED: exactly the layouts that section 2 dropped. Each refusal ends `Nothing was written`, the database (tables, columns, row counts and md5 of all rows, `om_meta`, ledgers) is the same after it, and the restored dump opens with the release.
- The mixed case passes for all 45 releases. In every release the session store fails on the runtime URL with `UndefinedColumn` (`column "session_id" referenced in foreign key constraint does not exist`); the branch starts runtime storage in place from v9, v10, v11, v13 or v15 to v15, and `storage migrate --module session` and the session store then use `public__session`.

Covered: runtime storage from every version a release left (v9, v10, v11, v13, v15); every module whose Alembic chain runs on PostgreSQL in that release (memory, a2a, session, registry, artifact, identity, policy, retrieve, secret, skill, task; no published release can migrate `compress`, `controlplane` or `telemetry` with Alembic); the memory, A2A audit and A2A state stores and the session, controlplane, telemetry, compress, registry, artifact, identity, policy, retrieve, skill and task stores; `storage migrate` with each store, in both orders.

## 4. PR gates on the PR head

Run [40914594792](https://github.com/AliceMessi/linux-runner/actions/runs/40914594792) (`linux/gates-40914594792/`). The checkout is the PR head `f04b4eae`; the runner's `make dev-install` installed it with the `postgres` extra. The script is [`probes/gates_446.sh`](probes/gates_446.sh), fetched from `wip/446-review-probes4`; each step's log is in `out/`.

| Step | Result |
| --- | --- |
| focused: the new and changed test files, the PostgreSQL conformance tests of every module store factory, `test_migrations_runner.py`, `test_startup_validation.py`, `test_storage_migrations_applied.py` | 247 passed, 1 failed: `test_unreachable_db_raises_with_redacted_url` times out at 60 s, as on `dev` (section 5) |
| the `postgres-memory-retrieval` job's ten commands, read from this head's `ci.yml` | 19, 1, 7, 6, 55, 8, 2, 13, 13 and 27 passed |
| `pytest -m postgres tests/` (without the benchmarks) | 290 passed, 2 skipped, 1 failed: `test_truth_maintenance.py::test_session_lifecycle_runs_capacity_and_purge_on_postgres`, which fails on `dev` `48332187` too (section 5) |
| non-slow markers on `tests/storage tests/integration tests/memory tests/a2a tests/session tests/cli tests/registry tests/skill tests/controlplane tests/artifact tests/identity tests/policy tests/retrieve tests/task tests/telemetry tests/compress tests/secret tests/api tests/runtime tests/services` | 8678 passed, 10 skipped, 1 xfailed, 1 failed: the same timeout |
| `ruff check .` | all checks passed |
| `make format-check` | 5165 files already formatted |
| `make lint` | passed; mypy ratchet 4306 / 4306 (`modules` 3344 / 3344) |
| dev control: the new and changed test files in a clean worktree of `dev` `48332187`, installed in place of the head | 42 failed, 71 passed, 1 collection error |

The dev control adds a `git worktree` of `48332187`, copies only the new and changed test files (and `tests/storage/postgres_test_utils.py`) into it (`out/dev-tree-status.txt`), and reinstalls the editable package from it (`out/dev-import.txt` shows `openminion` imported from that worktree). Its failures, by reason (`out/dev-failures.txt`):
- the code does not exist on `dev` (7, and the collection error): `tests/storage/test_postgres_schema.py` cannot import `postgres_schema`; four PostgreSQL tests import it; `MigrationRunner(select_postgres_schema=...)`, `SchemaOwnershipError` and `StorageEngine.from_config(module_id=...)` do not exist;
- the module writes into the base schema, so there is no `<base>__<module>` (16): `storage migrate` on a new database, `AppBase__secret`, the memory store's options, the startup check (shared `alembic_version`), the DBA-created `public__memory` (memory is denied `CREATE` in `public`), the runtime-memory capture, two migrate-report tests, and eight #521 baseline tests;
- the layout is accepted or changed instead of refused (17): the ten refused layouts, the three #521 foreign-table cases (#521's own error after the attempt instead of the refusal before it), R1's runtime start, `storage migrate` for memory, A2A, session and registry in one schema (exit 1 on the shared ledger), `storage migrate` after the session store tried the runtime URL, and the session store on the runtime URL (`UndefinedColumn`);
- `dev`'s docs have no `<!-- 446:R1b -->` and `<!-- 446:R2-park -->` blocks (2).

The dedicated memory database control passes on `dev`, as do the unchanged cases of the changed files.

## 5. Review probes

- Run [40900525510](https://github.com/AliceMessi/linux-runner/actions/runs/40900525510) (`linux/review-dev-40900525510/`), on `dev` `48332187` plus [`probes/leftover.py`](probes/leftover.py) only: runtime storage starts on a new database, then the session store is built on the same URL as `api/queries/sessions.py` builds it. It fails with `UndefinedColumn` (`column "session_id" referenced in foreign key constraint does not exist`) and leaves one index, `idx_sessions_status_updated`, on runtime storage's `sessions`; no table or column. The PR's `test_runtime_storage_after_the_session_store_tried_its_url` builds that layout and checks that it upgrades. The same run reproduces the two known failures on `dev` itself: `test_truth_maintenance.py::test_session_lifecycle_runs_capacity_and_purge_on_postgres` (`get('purge-me')` is not None) and `test_unreachable_db_raises_with_redacted_url` (60 s timeout).
- Run [40914749310](https://github.com/AliceMessi/linux-runner/actions/runs/40914749310) (`linux/pgbouncer-40914749310/`), [`probes/pgbouncer_probe.py`](probes/pgbouncer_probe.py) with Ubuntu's PgBouncer 1.22.0 in transaction pooling in front of the runner's server, on the PR head `f04b4eae` plus the probe (`wip/446-review-pgb2`). With PgBouncer's defaults, a connection with `options=-c search_path=...` is rejected (`unsupported startup parameter in options: search_path`), so the memory store cannot connect. With `ignore_startup_parameters = options`, and with `track_extra_parameters = search_path`, the connection is accepted but keeps `"$user", public`, and the memory store is refused with the PR's landing message. In every configuration the database is unchanged after the memory store attempt, and `storage migrate --module secret` through PgBouncer migrates into `public__secret`.

## Rerun

On a runner with PostgreSQL (`OPENMINION_TEST_POSTGRES_URL` a superuser URL), from a checkout that has the probes under `probe_tmp/`:

```bash
bash probe_tmp/446_histories/run_histories.sh "0.0.1 ... 0.1.15"   # measurement; then make_histories.py
bash probe_tmp/446/run_upgrade.sh "0.1.0 0.1.1 ..."                 # installed upgrade and restore
python -I probes/filter_histories.py histories-measured.json out.json upgrade-*.json
python -I probes/summarize_upgrade.py upgrade-*.json
bash gates_446.sh 4833218729abb9bbab4774a4f6141cfaacfa33a3        # on the PR head
```

## Not covered

- Dependencies of the published packages are those PyPI resolved on 29 Oct 2026, not the ones resolved when each release came out. The DDL is OpenMinion's own SQL text.
- Layouts that a published package never leaves as its end state are listed only when this code builds them itself.
- Mixed layouts are upgraded here only for runtime storage with the session store's attempt on its URL; the PR refuses the others, and its tests cover them.
- Windows, SQLite (unchanged by the PR), concurrent writers, poolers other than PgBouncer 1.22.

## Files

- `probes/`: `probe_446_histories.py`, `run_histories.sh`, `make_histories.py`, `probe_446_upgrade.py`, `run_upgrade.sh`, `filter_histories.py`, `summarize_upgrade.py`, `gates_446.sh`, `leftover.py`, `pgbouncer_probe.py`.
- `data/`: `histories-measured.json` (all 96), `postgres_histories.json` (the PR's 87), `filter-output.txt`, `upgrade-summary.txt`.
- `linux/<step>-<run id>/`: each run's `run.log`, `head.txt`, `result.txt`, `setup.log.gz` where kept, and `out/` (per release: probe logs, `*.json.gz`, `pip-freeze-*.txt`, install logs; for the gates, each step's log and the dev control's failures).
- `superseded-5fc5f9e5/`, `superseded-0c1b8ebd/`: the gates, installed-upgrade and PgBouncer runs of the heads before each review.
- `.gitattributes` (`* -text`); `SHA256SUMS.txt` last.
