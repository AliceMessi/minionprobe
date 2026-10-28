# OpenMinion: `storage migrate` entries without the migration error (#446)

The maintainer welcomed a focused PR for this part of [#446](https://github.com/openminion/openminion/issues/446) ([comment](https://github.com/openminion/openminion/issues/446#issuecomment-6459364446)). `_run_migrate_apply` (`cli/commands/storage.py`) built each entry from `report.success` only. A failed `MigrationReport` became `"status": "error"` with no message, and the text output printed only `FAIL <module>`. The entry's `applied` read `report.applied_versions`, which `MigrationReport` does not have, so it was `[]` in every entry.

The fix branch is `fix/storage-migrate-report-fields` @ `bddf6e88` (3 commits on `dev` @ `988ac4ec54ae803f4ac937a6fc2fc1c04bb5f590`). A failed report's entry carries `error`, and the text line prints it. `applied` is left out, and the exit status is unchanged.

## Probe

`probes/probe_446_report.py` runs `python -m openminion storage migrate` in subprocesses, with the tree under test on `PYTHONPATH`. It uses only new temporary files and databases, and it masks the password in URLs. There are three cases:
- `sqlite_unknown_revision`:
  1. `--module secret` on a new SQLite file;
  2. its `alembic_version` is set to `9999_not_a_revision`;
  3. `--module secret` again, once with `--json` and once without.
- `pg_session_then_artifact`: `--module session`, then `--module artifact`, on a new PostgreSQL database. This is the shared-`alembic_version` case from #446.
- `pg_unknown_revision`: the SQLite case on a new PostgreSQL database.

`probes/test_storage_migrate_report_fields.py` and `probes/test_storage_migrate_report_postgres.py` are copies of the PR's test files.

## Files

| sha256 | file |
|---|---|
| `9a581f263539d973d560474478ec447f89729933c0984a5bbf6ec82e20714ab5` | `probes/probe_446_report.py` |
| `92c52bb5c16f69d2b80a72ec7171dba9458954580495770acfd4b10236511867` | `probes/test_storage_migrate_report_fields.py` |
| `1620a3007e28979b2ba1f80db65b1528a29c0335ea8698e3b1b6f529de6cbb92` | `probes/test_storage_migrate_report_postgres.py` |
| `2f23989fee8fd360f9299c3fcd3b3ea90c8e68235fdd25ef86a396c6ff93aff8` | `linux/runner-gates.sh` |
| `10cef90584e7635ee34a1761c23286372f46c93168f2c10445c1bfd950023625` | `linux/runner-test-ci.sh` |
| `faa739cdb690fc5940e2d348e1fab04a4f2e3fe05f2c581987ec73b46bc7b414` | `linux/gates-run-40629517959.txt` |
| `5ea0e4aadd58ef540305dd14a167a7d935740b7245806e51067ec59e3384271c` | `linux/test-ci-run-40629518315.txt` |
| `d6b760aa91bbf725aacfc732410824b1ec50229b3fede2d534ee2984df5ff02a` | `windows/probe-head-bddf6e88.txt` |
| `e5fd0dd7a145c111693e1d8d68742ab0c1c46c9a4301a0747c979907bb7c001a` | `windows/probe-dev-988ac4ec-sources.txt` |
| `7e7aedfd4eb2d9bd994cc09078380a4c529e427fecd8176bcebbb868b16981b6` | `windows/pytest-head-bddf6e88.txt` |
| `7ba9e427b8b5ae2c508aaa820e20f1071464222670557404413a41b15f54b354` | `windows/pytest-dev-988ac4ec-sources.txt` |

- `linux/gates-run-40629517959.txt`: AliceMessi/linux-runner [run 40629517959](https://github.com/AliceMessi/linux-runner/actions/runs/40629517959), with the script `linux/runner-gates.sh`.
  - Environment: Ubuntu 24.04.5, CPython 3.11.17, SQLite 3.45.1, the `postgres` extra and a PostgreSQL 16 service.
  - The run does the same steps on the branch head and again after `git checkout 988ac4ec -- src`: the focused tests, `tests/cli` and `tests/storage`, the PostgreSQL tests, and the probe.
  - Back on the head it runs `ruff check .`, `make format-check`, `make workflow-check`, `make lint` and `make test-critical`.
- `linux/test-ci-run-40629518315.txt`: `make test-ci` on the head ([run 40629518315](https://github.com/AliceMessi/linux-runner/actions/runs/40629518315)), with the script `linux/runner-test-ci.sh`.
- `windows/`: Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4, a fresh `uv` venv with an editable install of the branch head.
  - The `dev` runs put a `git archive` of `988ac4ec`'s `src` first on `PYTHONPATH`. Paths are shortened to `<path>` and `<DEVSRC>`.
  - The probe ran without a PostgreSQL server, so its PostgreSQL cases are skipped there.

## Results

The `bddf6e88` column is the branch head; the `988ac4ec` column is the same steps after `git checkout 988ac4ec -- src`.

| Step | `bddf6e88` | `988ac4ec` sources |
|---|---|---|
| Probe, SQLite unknown revision | exit 1; `error`: `Alembic upgrade failed for module 'secret': Can't locate revision identified by '9999_not_a_revision'`; text `FAIL  secret: <that message>` | exit 1; `{"status": "error", "applied": [], "success": false}`, no message; text `FAIL  secret` |
| Probe, PostgreSQL `session` then `artifact` | exit 1; `error`: `... Can't locate revision identified by '0005_cron_scheduler_control'` | exit 1; no message |
| Probe, PostgreSQL unknown revision | exit 1; the message, as on SQLite | exit 1; no message |
| Focused tests (the new file and `tests/cli/test_storage_migration_commands.py`) | 27 passed | 5 failed (the new tests except the two controls), 22 passed |
| `-m postgres`: the new test and the three `test_postgres_tier*_promotion.py` files | 8 passed | 1 failed (the new test), 7 passed |
| `tests/cli tests/storage` (test-ci markers) | 1 failed, 2125 passed, 3 skipped | 6 failed (the 5 above and the same timeout), 2120 passed |
| `ruff check .`, `make format-check`, `make workflow-check`, `make lint` | pass | - |
| `make test-critical` | 486 passed, 4 skipped | - |
| `make test-ci` | 1 failed, 20420 passed, 29 skipped, 1 xfailed | - |

On Windows, the focused tests plus the PostgreSQL file (skipped) gave 27 passed and 1 skipped on the head. On `988ac4ec` sources they gave 5 failed, 22 passed and 1 skipped. The probe output matches the Linux SQLite case on both trees.

The one failure on the head is `tests/storage/test_startup_validation.py::TestValidatePostgresConnectionFailure::test_unreachable_db_raises_with_redacted_url`.
- With the `postgres` extra (psycopg) installed, it hits the 60 s pytest timeout connecting to `192.0.2.1`.
- On `988ac4ec` sources it fails the same way in the run above.
- Earlier runs showed the same timeout on `dev` with that extra ([run 40192315377](https://github.com/AliceMessi/linux-runner/actions/runs/40192315377)), and the test passing without it ([run 40192191864](https://github.com/AliceMessi/linux-runner/actions/runs/40192191864)).
