# OpenMinion: what a failed in-process `APIRuntime` start leaves open

The second "Also observed" item of [openminion#437](https://github.com/openminion/openminion/issues/437), filed separately as the maintainer asked. [#390](https://github.com/openminion/openminion/issues/390) (PR #432) covered a construction failure inside the memory assembly only. This folder looks at the whole start: `APIRuntime.from_config_path` raises, and the probe records what the process still holds.

## What ran

- Native Windows 11 (CPython 3.12.11): the three SQLite cases, with two venvs:
  - `dev`: `openminion[postgres] @ git+https://github.com/openminion/openminion@68d623d10317a7cea13832f8dbd51fc98a871aa5` ([`windows/direct_url-dev.json`](windows/direct_url-dev.json)). Its version string is 0.1.13;
  - `0.1.13`: `openminion[postgres]==0.1.13` from PyPI.
- Linux: one runner job, [40502798592](https://github.com/AliceMessi/linux-runner/actions/runs/40502798592), on 27 Oct 2026. It is a GitHub-hosted Ubuntu 24.04.5 machine with CPython 3.12.15 and PostgreSQL 16.15 in Docker. [`probes/linux_setup.sh`](probes/linux_setup.sh) creates the same two venvs and runs every case. The same job also ran `probe_migrate_ledger.py` for [`openminion-storage-migrate-ledger`](../openminion-storage-migrate-ledger).
- [`probes/probe_failed_start_retention.py`](probes/probe_failed_start_retention.py) runs with `python -I -X utf8` from an empty directory. Each case runs in its own child process. The child turns automatic garbage collection off before it imports openminion. So the first snapshot ("at the exception") shows what reference counting alone releases, and the second one comes after an explicit `gc.collect()`.
- Every config file has one offline `echo` agent and the storage section, as in #437.
- A snapshot records:
  - the files under the case root that the process holds open (`psutil`);
  - on Windows, which of them cannot be renamed at that moment;
  - the threads the attempt started that are still alive;
  - how many times `RuntimeStorageContext.close()` and `close_runtime_components()` ran;
  - for PostgreSQL, how many other connections to the case database are open (`pg_stat_activity`, from a separate connection).
- At the end, the probe closes what is still open with the existing `close()` methods, as cleanup. Then it takes one more snapshot. This shows which object holds the resources. It is not runtime behaviour.

## Cases

| Case | Trigger | Raises |
|---|---|---|
| `sqlite_started_then_closed` (control) | SQLite runtime storage. The start succeeds, then `runtime.close()` | none |
| `sqlite_memory_provider` | SQLite runtime storage and `OPENMINION_MEMORY_PROVIDER=not-a-provider`. The memory assembly reads this variable after the runtime storage, the telemetry store, and the policy, ops and retrieve stores were built | `ConfigError: Unsupported runtime.memory_provider='not-a-provider'...` at `modules/memory/runtime/configuration.py:117` |
| `sqlite_437_point` | SQLite runtime storage. The probe patches `RuntimeStorageContext.connection` to raise the #437 `RuntimeError`, so the start fails where #437 fails on PostgreSQL (fault injection) | `RuntimeError` at `api/core/bootstrap.py:126` |
| `postgres_437` | `storage.backend: postgres` on a new database (#437) | `RuntimeError` at `api/core/bootstrap.py:126` (`modules/storage/runtime/context.py:54`) |
| `postgres_runtime_migration` | `storage.backend: postgres` on a new database that already has a `sessions` table | `MigrationError: Migration v1 (bootstrap_core_tables) failed: ... relation "sessions" already exists` at `modules/storage/runtime/migrations.py:769`, inside `build_runtime_storage` |
| `postgres_storage_closed` (control) | `build_runtime_storage` on PostgreSQL with the runtime's telemetry hook, then `close()` | none |

## Results

`dev` and 0.1.13 gave the same results, on Windows and on Linux. In every failing case the caller got the exception above unchanged. `RuntimeStorageContext.close()` and `close_runtime_components()` ran 0 times.

| Case | At the exception (automatic gc off) | After `gc.collect()` | After the probe's cleanup |
|---|---|---|---|
| `sqlite_started_then_closed` | after `runtime.close()`: 0 open files under the data root; Windows: every file can be renamed | same | not needed |
| `sqlite_memory_provider` | 7 databases open (15 files with `-wal`/`-shm`): `state.db`, `telemetry/telemetry.db`, `policy/policy.db`, `ops/evidence.db`, `ops/jobs.db`, `ops/plans.db`, `retrieve/retrieve.db`. Windows: none of the 15 can be renamed (`PermissionError [WinError 32]`) | 0 open; all can be renamed | not needed |
| `sqlite_437_point` | 9 databases (21 files): the 7 above, `artifact/index.db` and `authored_tools/store.sqlite`. Windows: none can be renamed | `artifact/index.db` (3 files) still open; Windows: cannot be renamed | not run (no runtime storage object left) |
| `postgres_437` (Linux) | 1 server connection (0 before). Thread `storage-pool-health-emitter` running. 8 SQLite databases open: the 9 above without `state.db` | 1 server connection; the thread is still running; `telemetry/telemetry.db` and `artifact/index.db` open | `PoolHealthEmitter.stop()` and `RecordStorePostgres.close()`: 0 connections, no thread, only `artifact/index.db` open |
| `postgres_runtime_migration` (Linux) | 1 server connection (0 before). Thread `storage-pool-health-emitter` running. `telemetry/telemetry.db` open | same | stopping the emitter and closing the record store it holds: 0 connections, no thread, nothing open |
| `postgres_storage_closed` | 1 server connection while open; after `close()`: 0, no thread | 0 | not needed |

What holds them:
- PostgreSQL: after `gc.collect()`, the record store's only referrers are the `PoolHealthEmitter` and a closure cell. The emitter's daemon thread keeps running. Through the storage telemetry hook it also keeps the `TelemetryService` alive, which is why `telemetry/telemetry.db` stays open.
- `artifact/index.db`: the memory store is reached from the module-level backend factory registry. `_BACKEND_FACTORIES["sophiagraph"]` closes over the `AuditedMemoryStore` (`memory_backend_registry_sophiagraph_closes_over` in the JSON). After a successful start and `runtime.close()`, the registry holds the same kind of store, but its files are closed.
- SQLite `state.db` and the other module databases: after the exception nothing reachable refers to them, but reference cycles keep them alive. Reference counting does not free them; `gc.collect()` does.

## Minimal reproduction

[`minimal-repro/repro_failed_start.py`](minimal-repro/repro_failed_start.py) is the script in the report. It writes the minimal config, starts `APIRuntime` once with automatic gc off, and prints what is open after the exception and after `gc.collect()`. With one argument it uses SQLite runtime storage and `OPENMINION_MEMORY_PROVIDER=not-a-provider`. With a PostgreSQL URL as the second argument it uses the #437 config.

- Windows, both builds ([`minimal-repro/windows/`](minimal-repro/windows), raw stdout): `ConfigError`. After the exception, 15 open files under the root (7 databases) and `renaming state.db: PermissionError [WinError 32]`. After `gc.collect()`: 0 open files, and renaming `state.db` works.
- Linux, both builds, runner job [40504243914](https://github.com/AliceMessi/linux-runner/actions/runs/40504243914) ([`minimal-repro/linux_setup.sh`](minimal-repro/linux_setup.sh); outputs in [`minimal-repro/linux/`](minimal-repro/linux), with the library log in the `.stderr.txt` files):
  - SQLite: the same 7 databases open after the exception (16 entries; `state.db` is open twice), 0 after `gc.collect()`;
  - PostgreSQL: `RuntimeError: runtime record_store does not expose sqlite connection`. After the exception, 18 open files (8 databases) and 1 other connection to the database. After `gc.collect()`, `artifact/index.db` and `telemetry/telemetry.db` are still open, and there is still 1 connection.

The same job also ran the storage-migrate reproduction for [`openminion-storage-migrate-ledger`](../openminion-storage-migrate-ledger); `linux_setup.sh` and [`minimal-repro/linux/repro-sha256.txt`](minimal-repro/linux/repro-sha256.txt) cover both.

| sha256 | file |
|---|---|
| `da974dfc1e4b4e621a2e3acb220faa40d0a819ecef32265b50f29e703c4b0cdb` | `minimal-repro/repro_failed_start.py` |
| `2b791c419c767161c758d2cccbe6c021a9c0fe3904dac5598331e14fe1a51679` | `minimal-repro/linux_setup.sh` |

## Files

- `probes/`: the probe and the runner setup, byte-identical to what ran on both machines:

  | sha256 | file |
  |---|---|
  | `6d556cd1314c160f6b728b5d7c0612bb74519cd824ec3c050c7759f1d7716195` | `probe_failed_start_retention.py` |
  | `96ade72c192994d70a84b10e85abf9dcd96729c1e9a8f406292d159a7b584595` | `linux_setup.sh` |

  [`linux/probe-sha256.txt`](linux/probe-sha256.txt) is the runner's own hash of the files in the job. It also lists `probe_migrate_ledger.py`, which is in [`openminion-storage-migrate-ledger/probes`](../openminion-storage-migrate-ledger/probes). [`windows/probe-sha256.txt`](windows/probe-sha256.txt) is the hash of the probe that ran on Windows.
- `windows/`: `probe_failed_start_retention_<build>.json` and `.log` (the raw output, CRLF), `pip-list-<build>.txt`, `direct_url-dev.json`, `environment.txt`.
- `linux/`: `probe_failed_start_retention_<build>.json` and `.log`, `pip-show-*.txt`, `pip-list-*.txt`, `dev-commit.txt`, `os.txt`, `postgres-version.txt` and `run-id.txt`.
- In the JSON, every case has `at_failure` (or `after_close`), `after_gc_collect` and, where it ran, `probe_cleanup` and `after_probe_cleanup`. `child_stderr_tail` holds the end of the library log.
- `.gitattributes` (`* -text`): the files are published byte for byte.
