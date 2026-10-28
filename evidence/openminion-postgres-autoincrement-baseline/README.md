# OpenMinion: the controlplane and telemetry baselines fail on PostgreSQL (`AUTOINCREMENT`)

Promised in the revised #446 proposal ("Separate reports", item 2); separate from its table-collision fix. The issue draft states the bug, the code and the possible fix. This folder holds what was run.

`openminion storage migrate --backend postgres --module controlplane` and `--module telemetry` exit 1 with `(psycopg.errors.SyntaxError) syntax error at or near "AUTOINCREMENT"`. Both modules' `0001_baseline` apply SQLite DDL (`INTEGER PRIMARY KEY AUTOINCREMENT`) on every dialect. Both modules are in `POSTGRES_VALIDATED_MODULES`.

## What ran

Two jobs on [AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner), 28 Oct 2026. Each is a GitHub-hosted Ubuntu 24.04.5 machine with PostgreSQL 16.15 (`postgres:16` in Docker). Both builds used SQLAlchemy 2.1.6, psycopg 3.3.6, Alembic 1.18.4 and SQLite 3.45.1.

| Build | Run | How it was installed |
|---|---|---|
| dev `39da8ffb` | [40735178351](https://github.com/AliceMessi/linux-runner/actions/runs/40735178351) | Checkout of AliceMessi/openminion @ `0891341a`: dev `39da8ffb09920792d2de269b0ad830e0623a65c9` plus a `probe_tmp/` folder with the probe and `probes/run_dev.sh`. Nothing else changed (`linux/dev/dev-commit.txt`). The runner's `openminion` preset made `.venv` (editable install, `postgres` extra). CPython 3.11.17. |
| 0.1.14 | [40735247701](https://github.com/AliceMessi/linux-runner/actions/runs/40735247701) | `openminion[postgres]==0.1.14` from PyPI in a new `uv` venv, on a machine with no openminion checkout (`probes/linux_setup.sh`, `probes/steps_0114.sh`). CPython 3.12.15. |

[`probes/probe_postgres_autoincrement_baseline.py`](probes/probe_postgres_autoincrement_baseline.py) ran from an empty directory with `python -I -X utf8`. The CLI is the installed `openminion` console script, run as a subprocess. Every PostgreSQL case uses a new, empty database, and every SQLite case a new file. Passwords in URLs are masked.

On dev, `--json` entries carry the migration error (#457). On 0.1.14 they do not. So after each failed CLI call, the probe also runs the same migration in process with the CLI's own helpers (`_make_runner` and `MigrationRunner.migrate`) and prints `report.error`. The failed CLI attempt was rolled back, so the second attempt starts from the same state.

## Results

dev and 0.1.14 gave the same results in every case. The only difference is the missing message in the 0.1.14 entries.

| Case | Result (dev `39da8ffb` and 0.1.14) |
|---|---|
| Installed `migrations/versions/*.py` (the dev tree has 43), lines with `AUTOINCREMENT` | 5: compress `0001_baseline.py:35`, controlplane_telegram `0001_baseline.py:34`, controlplane `0001_baseline.py:51` and `:90`, telemetry `0001_baseline.py:15` |
| The `DDL` tuples those revisions apply, with imported statements | controlplane: `cp_inbound_messages`, `cp_outbound_messages` and `cp_pair_attempts` (imported from `controlplane/pairing/schema.py`), no PostgreSQL variant. telemetry: `events`, no PostgreSQL variant. compress: `dropped_reasons`, but it has a `POSTGRES_DDL`. controlplane_telegram: `telegram_pair_attempts`, no PostgreSQL variant, not PostgreSQL-validated. |
| `storage migrate --backend postgres --postgres-url <new db> --module controlplane --json` | exit 1. dev entry: `"error": "Alembic upgrade failed for module 'controlplane': (psycopg.errors.SyntaxError) syntax error at or near \"AUTOINCREMENT\"\nLINE 2:         id INTEGER PRIMARY KEY AUTOINCREMENT, ... [SQL: CREATE TABLE IF NOT EXISTS cp_inbound_messages (...)]"`. 0.1.14 entry: no message; the same message in process. Database afterwards: no tables. |
| The same with `--module telemetry` | exit 1, the same error on `CREATE TABLE IF NOT EXISTS events (...)`. Database afterwards: no tables. |
| Text mode (no `--json`) | exit 1. dev: `FAIL  controlplane: Alembic upgrade failed ...` with the message; 0.1.14: `FAIL  controlplane`. The same for telemetry. |
| Control in the same run: `--module secret` | exit 0, `ok`, `0001_baseline`, 3 tables |
| SQLite: `storage migrate --sqlite <new file> --module controlplane` / `--module telemetry` | exit 0, `ok`. controlplane at `0002_index_audit` (20 tables with `sqlite_sequence`); telemetry at `0003_debug_query_indexes` (4 tables) |
| Each module that the CLI runs on PostgreSQL, alone, `--module <id>`, each on its own new database | 11 `ok`. `controlplane` and `telemetry`: the syntax error. `compress`: `Alembic configuration missing` (reported separately). |
| `storage migrate --backend postgres` (every module) on one new database | exit 1. 5 `skipped`, `session` `ok`. `controlplane`, `telemetry` and 10 others: `Can't locate revision identified by '0005_cron_scheduler_control'` (the shared `alembic_version`, #446). `compress`: `Alembic configuration missing`. The loop runs every module; it does not stop at the first failure. |
| Each statement of the shipped controlplane `0001_baseline` DDL (44), run on its own on a new database | 10 fail: `CREATE TABLE` of `cp_inbound_messages`, `cp_outbound_messages` and `cp_pair_attempts` with `SyntaxError: syntax error at or near "AUTOINCREMENT"`, and the 7 indexes on those three tables with `UndefinedTable`. The other 34 succeed. |
| The same for telemetry (4 statements) | 3 fail: `CREATE TABLE events` (`SyntaxError`) and its 2 indexes (`UndefinedTable`) |
| The same statements with `INTEGER PRIMARY KEY AUTOINCREMENT` replaced by `SERIAL PRIMARY KEY` (what `PostgresControlPlaneStore` already does) | 0 fail, for both modules |
| PostgreSQL: `CAST(1761650000.123456 AS REAL)` read back as double precision | `1761650048.0`; as `DOUBLE PRECISION`: `1761650000.123456` |
| Scratch copy of each module's `migrations/`, with one `upgrade()` appended to `0001_baseline.py` that uses `SERIAL PRIMARY KEY` and, for telemetry, `timestamp DOUBLE PRECISION` on PostgreSQL only (the patch text is in the JSON under `patched_copy.patch`); `MigrationRunner` with that script location on a new database | controlplane: `success=True`, `0002_index_audit`, 19 tables; two rows inserted without `id` got ids `[1, 2]`; `id` default `nextval('cp_inbound_messages_id_seq'::regclass)`. telemetry: `success=True`, `0003_debug_query_indexes`; a timestamp of `1761650000.123456` read back unchanged; `id` default `nextval('events_id_seq'::regclass)`. |
| SQLite, the same scratch copy | New file: shipped scripts and scratch copy both succeed, `sqlite_master` identical. A file migrated with the shipped scripts, then `upgrade head` with the scratch copy: success, `alembic_version` unchanged (`0002_index_audit` / `0003_debug_query_indexes`), `sqlite_master` unchanged. Both modules. |
| `build_runtime_storage` with `record_backend="record.postgres"` on a new database | builds, runtime schema v15. It logs 14 warnings, among them `Postgres module 'controlplane' has pending migrations. Run: openminion storage migrate --module controlplane` and the same for `telemetry`. |
| Those commands on the same database (`--backend postgres --postgres-url <url> --module controlplane` / `telemetry`) | exit 1, the same syntax error |

## How to rerun

Without our runner, from an empty directory, with a PostgreSQL server URL in `PGURL`:

```bash
# dev: a checkout of 39da8ffb with `pip install -e ".[postgres]"`; or the release:
pip install "openminion[postgres]==0.1.14"
PGURL='postgresql+psycopg://postgres:<password>@localhost:5432/postgres' \
  python -I -X utf8 probe_postgres_autoincrement_baseline.py <out_dir> <label>
```

The probe creates databases named `pai_<label>_*` (it drops a database of the same name first) and temporary files only. The scratch copies of the migrations are made in a temporary directory; the installed package is not changed.

## Files

- `probes/`: the probe, `run_dev.sh` (the dev job's script), `linux_setup.sh` and `steps_0114.sh` (the 0.1.14 job). The runner's own hashes of what it ran are in `linux/dev/probe-sha256-dev.txt` and `linux/0.1.14/probe-sha256-0.1.14.txt`.

  | sha256 | file |
  |---|---|
  | `95f072efae6785cbfff2db88f95742216f4a9c7d8b5ac167abfc0743633350ba` | `probes/probe_postgres_autoincrement_baseline.py` |
  | `447e9f9d2f2fb242908119e3d3a4e64f44e95394a006c8b3699bd8a7915ac396` | `probes/run_dev.sh` |
  | `8a68c0484527c4527c5a51b947f6d0eaeeea1d2b38959804c4fb4c004d95c1fb` | `probes/linux_setup.sh` |
  | `8ce43ea47e905353de1dd86ce88fdceddd7f52d2ce49ac48376468585e7924e1` | `probes/steps_0114.sh` |

- `linux/dev/` and `linux/0.1.14/`: the job outputs.
  - `probe_postgres_autoincrement_baseline_<build>.log`: one `CASE` line per step.
  - `probe_postgres_autoincrement_baseline_<build>.json`: argv (password masked), exit codes, entries, in-process messages, the failing statements, the scratch-copy patch and checks, and the database facts after each step.
  - `pip-show-*`, `pip-list-*`, `os-*`, `postgres-version-*`, `run-id.txt`, `result.txt` (the job's exit and duration) and `runner-head.txt` (the commit the job checked out). dev also has `dev-commit.txt`.
- `SHA256SUMS.txt`: every file in this folder.
- `.gitattributes` (`* -text`): the files are published byte for byte.
