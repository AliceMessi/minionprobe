# OpenMinion: `storage migrate --module compress` finds no Alembic configuration

Promised in the revised #446 proposal ("Separate reports", item 1); separate from its table-collision fix. The issue draft states the bug, the code and the possible fix. This folder holds what was run.

`openminion storage migrate --module compress` fails with `Alembic configuration missing. Provide alembic_ini_path or alembic_script_location.` on PostgreSQL and on SQLite. The compress storage package is `openminion.modules.context.compress.storage`. `discover_module_storage_root("compress")` tries `openminion.modules.compress.storage`, which does not exist, and returns `None` (`modules/storage/migrations/alembic.py:14-47` on dev `39da8ffb`).

## What ran

Two jobs on [AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner), 28 Oct 2026. Each is a GitHub-hosted Ubuntu 24.04.5 machine with PostgreSQL 16.15 (`postgres:16` in Docker). Both builds used SQLAlchemy 2.1.6, psycopg 3.3.6, Alembic 1.18.4 and SQLite 3.45.1.

| Build | Run | How it was installed |
|---|---|---|
| dev `39da8ffb` | [40735165243](https://github.com/AliceMessi/linux-runner/actions/runs/40735165243) | Checkout of AliceMessi/openminion @ `f96e1dd0`: dev `39da8ffb09920792d2de269b0ad830e0623a65c9` plus a `probe_tmp/` folder with the probe and `probes/run_dev.sh`. Nothing else changed (`linux/dev/dev-commit.txt`). The runner's `openminion` preset made `.venv` (editable install, `postgres` extra). CPython 3.11.17. |
| 0.1.14 | [40735231199](https://github.com/AliceMessi/linux-runner/actions/runs/40735231199) | `openminion[postgres]==0.1.14` from PyPI in a new `uv` venv, on a machine with no openminion checkout (`probes/linux_setup.sh`, `probes/steps_0114.sh`). CPython 3.12.15. |

[`probes/probe_compress_alembic_config.py`](probes/probe_compress_alembic_config.py) ran from an empty directory with `python -I -X utf8`. The CLI is the installed `openminion` console script, run as a subprocess. Every PostgreSQL case uses a new, empty database, and every SQLite case a new file. Passwords in URLs are masked.

On dev, `--json` entries carry the migration error (#457). On 0.1.14 they do not. So after each failed CLI call, the probe also runs the same migration in process with the CLI's own helpers (`_make_runner` and `MigrationRunner.migrate`) and prints `report.error`. The failed CLI attempt was rolled back, so the second attempt starts from the same state.

## Results

dev and 0.1.14 gave the same results in every case. The only difference is the missing message in the 0.1.14 entries.

| Case | Result (dev `39da8ffb` and 0.1.14) |
|---|---|
| `discover_module_storage_root(m)` and `discover_module_alembic_paths(m)` for each of the 19 module ids | A script location for 16 ids. None for `compress` (storage root `None`), `storage` (storage root `None`) and `brain` (storage root found, no `migrations/env.py` in it). The CLI skips `storage` and `brain` on PostgreSQL. |
| Installed `*/storage/migrations/env.py` packages, checked against discovery for their `MODULE_ID` | 17 packages; 16 found. Not found: `openminion.modules.context.compress.storage` (`MODULE_ID = "compress"`). |
| `import openminion.modules.compress.storage` | `ModuleNotFoundError: No module named 'openminion.modules.compress'` |
| `import openminion.modules.context.compress.storage` | imports |
| `storage migrate --backend postgres --postgres-url <new db> --module compress --json` | exit 1. dev: `{"module_id": "compress", "status": "error", "success": false, "error": "Alembic configuration missing. Provide alembic_ini_path or alembic_script_location."}`. 0.1.14: `{"module_id": "compress", "status": "error", "applied": [], "success": false}`, and the same message in process. Database afterwards: no tables. |
| The same without `--json` | exit 1. dev: `FAIL  compress: Alembic configuration missing. Provide alembic_ini_path or alembic_script_location.` 0.1.14: `FAIL  compress` |
| Control in the same run: `--module secret` on a new database | exit 0, `ok`. Tables `alembic_version`, `om_meta`, `secrets`; `alembic_version` = `0001_baseline`; `om_meta.module_id` = `secret` |
| Each module that the CLI runs on PostgreSQL, alone, `--module <id>`, each on its own new database | 11 `ok`. `compress`: the message above. `controlplane` and `telemetry`: `(psycopg.errors.SyntaxError) syntax error at or near "AUTOINCREMENT"` (reported separately). |
| `storage migrate --backend postgres` (every module) on one new database | exit 1. 5 `skipped`, `session` `ok`, `compress` with the message above. The other 12 modules: `Can't locate revision identified by '0005_cron_scheduler_control'` (the shared `alembic_version`, #446). The loop runs every module; it does not stop at the first failure. |
| `MigrationRunner(module_id="compress", backend_type="postgres", alembic_script_location=<compress storage>/migrations)` on a new database | `success=True`. `alembic_version` = `0001_baseline`; tables `compression_failures`, `compression_runs`, `dropped_reasons`, `om_meta`; `dropped_reasons.id` is an identity column; `om_meta.module_id` = `compress`. dev also passed the checkout's `alembic.ini`; the 0.1.14 wheel ships no `alembic.ini` for any module, so it ran with the script location only. |
| SQLite: `storage migrate --sqlite <new file> --module compress --json` | exit 1, the same message (dev entry; 0.1.14 in process). The file is unchanged: no tables, `application_id` 0. |
| SQLite control: `--module secret` | exit 0, `ok`, `0001_baseline`, `application_id` `0x4f4d0006` |
| SQLite: compress's own `openminion.modules.context.compress.storage.migrations.run_migrations(<new file>)` | `RuntimeError: Alembic configuration missing. Provide alembic_ini_path or alembic_script_location.` |
| SQLite: `MigrationRunner` for compress with the explicit script location | `success=True`, `0001_baseline`, `application_id` `0x4f4d000e` |
| `build_runtime_storage` with `record_backend="record.postgres"` on a new database | builds, runtime schema v15. It logs 14 warnings, one per validated module, among them `Postgres module 'compress' has pending migrations. Run: openminion storage migrate --module compress`. |
| That command on the same database (`--backend postgres --postgres-url <url> --module compress --json`) | exit 1, the same message |

## How to rerun

Without our runner, from an empty directory, with a PostgreSQL server URL in `PGURL`:

```bash
# dev: a checkout of 39da8ffb with `pip install -e ".[postgres]"`; or the release:
pip install "openminion[postgres]==0.1.14"
PGURL='postgresql+psycopg://postgres:<password>@localhost:5432/postgres' \
  python -I -X utf8 probe_compress_alembic_config.py <out_dir> <label>
```

The probe creates databases named `cac_<label>_*` (it drops a database of the same name first) and temporary files only.

## Files

- `probes/`: the probe, `run_dev.sh` (the dev job's script), `linux_setup.sh` and `steps_0114.sh` (the 0.1.14 job). The runner's own hashes of what it ran are in `linux/dev/probe-sha256-dev.txt` and `linux/0.1.14/probe-sha256-0.1.14.txt`.

  | sha256 | file |
  |---|---|
  | `68f97f4996d6c12d7fb8dd945795d4709237a580cf323f5970e3e32006badb30` | `probes/probe_compress_alembic_config.py` |
  | `7bc769bb3beccddbc09dfa4d1921bb943c67100b285034b962bd21377dac85e2` | `probes/run_dev.sh` |
  | `8a68c0484527c4527c5a51b947f6d0eaeeea1d2b38959804c4fb4c004d95c1fb` | `probes/linux_setup.sh` |
  | `9db67081fe2420890b6e6322c09efd19d9a15bcd6ea92fb36cb69e757b510c49` | `probes/steps_0114.sh` |

- `linux/dev/` and `linux/0.1.14/`: the job outputs.
  - `probe_compress_alembic_config_<build>.log`: one `CASE` line per step.
  - `probe_compress_alembic_config_<build>.json`: argv (password masked), exit codes, entries, text output, in-process messages and the database facts after each step.
  - `pip-show-*`, `pip-list-*`, `os-*`, `postgres-version-*`, `run-id.txt`, `result.txt` (the job's exit and duration) and `runner-head.txt` (the commit the job checked out). dev also has `dev-commit.txt`.
- `SHA256SUMS.txt`: every file in this folder.
- `.gitattributes` (`* -text`): the files are published byte for byte.
