# OpenMinion: `APIRuntime` on a PostgreSQL runtime database

The separate startup report requested on [openminion#369](https://github.com/openminion/openminion/issues/369#issuecomment-6417425007), linked from [#364](https://github.com/openminion/openminion/issues/364). During the 0.1.12 published-package verification ([`openminion-0.1.12-verification`](../openminion-0.1.12-verification), #364 and #369), `APIRuntime.from_config_path` with `storage.backend: postgres` did not start. This folder records that startup on its own. A minimal config file, the startup trace, and the controls ran on the published 0.1.11 and 0.1.12 packages and on `dev` @ `773491f2`.

## What ran

One runner job, [40336017992](https://github.com/AliceMessi/linux-runner/actions/runs/40336017992), on 26 Oct 2026. The runner is a GitHub-hosted Ubuntu 24.04.5 machine ([AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner), preset `none`) with PostgreSQL 16.15 in Docker.

- [`probes/runner_setup.sh`](probes/runner_setup.sh) creates three new venvs (`uv venv --python 3.12`):
  - `openminion[postgres]==0.1.11` and `==0.1.12` from PyPI;
  - `openminion[postgres] @ file://<checkout>` for `openminion/openminion` @ `773491f299815677e3dc0872a0ddc79a769d0927`, the `dev` head after #428. Its version string is 0.1.12.
- Each venv runs [`probes/probe_apiruntime_pg.py`](probes/probe_apiruntime_pg.py) with `python -I` from an empty directory.
- Every case uses a new, empty PostgreSQL database and a hand-written config file. The file has one offline `echo` agent and the storage section. The PostgreSQL URL has the form `postgresql+psycopg://postgres:***@localhost:5432/<new database>`.
- For the PostgreSQL cases, the probe records the database after the attempt:
  - the runtime `migrations` ledger;
  - the number of tables in `public`;
  - how many other connections to that database the probe process still holds after `gc.collect()`.

An earlier job, [40335531003](https://github.com/AliceMessi/linux-runner/actions/runs/40335531003), ran the first eight cases with the same results. It is superseded because it did not have the last two cases, and it is not used.

The minimal config, as the PostgreSQL case wrote it (the paths are sanitized):

```json
{
  "default_agent": "first",
  "agents": {"first": {"provider": "echo", "default_channel": "console"}},
  "runtime": {"process_mode": "single-process", "daemon_auto_start": false},
  "storage": {
    "path": "<RUNNER_TEMP>/work-0.1.12/postgres_api/.openminion/state.db",
    "backend": "postgres",
    "postgres_url": "postgresql+psycopg://postgres:***@localhost:5432/om_0_1_12_api"
  }
}
```

## Results

All three builds gave the same outcome. The only difference is the runtime migration head: 13 on 0.1.11, 15 on 0.1.12 and `dev`.

| Case | 0.1.11 | 0.1.12 | dev `773491f2` |
|---|---|---|---|
| `APIRuntime.from_config_path`, SQLite config (control) | started (`RecordStoreSQLite`, `sqlite3.Connection`) | started | started |
| Same, with `storage.backend: postgres` and `storage.postgres_url` | `RuntimeError: runtime record_store does not expose sqlite connection` (`api/core/bootstrap.py:126`) | same | same |
| Same, SQLite config file with `OPENMINION_STORAGE_BACKEND=postgres` and `OPENMINION_STORAGE_POSTGRES_URL` | same `RuntimeError` | same | same |
| Same, `postgresql://` URL (no driver name) | same `RuntimeError` | same | same |
| Runtime database after the failed start: `migrations` rows, max version, tables | 13, v13, 18 tables (empty before) | 15, v15, 19 | 15, v15, 19 |
| Other connections to that database still open in the probe process after the failure and `gc.collect()` | 1 (0 before) | 1 | 1 |
| `build_runtime_storage` with the same backend and options, then `.connection` | `RecordStorePostgres`, `SessionStore`; `.connection` raises the same `RuntimeError` | same | same |
| `openminion ... room create --name probe-room --agent first`, SQLite (control) | exit 0, `room=room-...`, `participants=1` | exit 0 | exit 0 |
| Same, PostgreSQL config | exit 1, `openminion room: startup error — runtime record_store does not expose sqlite connection` | same | same |
| `openminion ... sessions list --json`, PostgreSQL config (control: `build_runtime_storage` without `APIRuntime`) | exit 0, `[]`, plus 14 `Postgres module '<id>' has pending migrations` warnings on stderr | same | same |
| `openminion storage migrate --backend postgres --postgres-url <url> --json` on a new database, then `APIRuntime.from_config_path` | migrate exit 1 (`session` ok, 13 modules `error` with no message, 5 skipped); then `MigrationError: Migration v1 (bootstrap_core_tables) failed: (psycopg.errors.DuplicateTable) relation "sessions" already exists` | same | same |

The trace on 0.1.12 (0.1.11 and `dev` have the same frames and line numbers):

```
Traceback (most recent call last):
  File "/tmp/b/probe_apiruntime_pg.py", line 190, in try_api_runtime
    runtime = APIRuntime.from_config_path(str(config_path), **roots(root))
  File "<VENV>/lib/python3.12/site-packages/openminion/api/core/bootstrap.py", line 191, in from_config_path
    return cls.from_manager(
  File "<VENV>/lib/python3.12/site-packages/openminion/api/core/bootstrap.py", line 235, in from_manager
    return finalize_runtime_instance(
  File "<VENV>/lib/python3.12/site-packages/openminion/api/core/bootstrap.py", line 126, in finalize_runtime_instance
    storage_connection=runtime_storage.connection,
  File "<VENV>/lib/python3.12/site-packages/openminion/modules/storage/runtime/context.py", line 54, in connection
    raise RuntimeError("runtime record_store does not expose sqlite connection")
RuntimeError: runtime record_store does not expose sqlite connection
```

Before the failure, `build_runtime_infrastructure` has already built the PostgreSQL runtime storage and applied the runtime migrations (`modules/storage/runtime/context.py`, `migrate_record_store`). The probe log shows what startup did next:
- it logged 14 `Postgres module '<id>' has pending migrations` warnings;
- it loaded 104 tools;
- it ran two Alembic upgrades and the memory-store migrations on SQLite;
- it selected the SQLite `retrieve` provider;
- it initialized the agent bridge.

After that, `finalize_runtime_instance` reads `runtime_storage.connection`.

## Files

- `probes/`: the probe and the runner setup, byte-identical to what the runner ran:

  | sha256 | file |
  |---|---|
  | `f67b46decf33bb616515fa335d1e01467a16c6e7e663158ed8a7caeab8a35cf4` | `probe_apiruntime_pg.py` |
  | `6af360dbcd0a1177cc29a909446447647734daf35361dfe4c2238d24b29fa76b` | `runner_setup.sh` |

  [`linux/probe-sha256.txt`](linux/probe-sha256.txt) is the runner's own hash of the same files.
- `linux/`:
  - `probe_<version>.log`: one `CASE` line per case, the full traces and the library log;
  - `probe_<version>.json`: the configs (password masked), the exceptions, the traces, the CLI exit codes and output, and the database facts;
  - `pip-show-*.txt` and `pip-list-*.txt`: the three venvs;
  - `dev-commit.txt`, `os.txt`, `postgres-version.txt` and `run-id.txt`.
- `.gitattributes` (`* -text`): the files are published byte for byte.
