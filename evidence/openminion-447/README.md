# OpenMinion #447: a failed `APIRuntime` start closes what it built, the fix

Validation of the PR for [openminion#447](https://github.com/openminion/openminion/issues/447), as proposed there ([comment](https://github.com/openminion/openminion/issues/447#issuecomment-6460713785)) and approved ([comment](https://github.com/openminion/openminion/issues/447#issuecomment-6465004005)). Branch `fix/447-failed-start-cleanup`: three commits (test, fix, docs and changelog) on `dev` @ `042e0a2c`, ending at `20034752`.

Before the fix, a start step that raised left open everything the start had built: on SQLite the runtime database and the module databases, on PostgreSQL also the record store's pool and its pool-health emitter thread, which survived `gc.collect()`. With the fix, each owner closes what it acquired when a later step inside it fails, with the existing closes, and re-raises the original error. Once the runtime object exists, a later failure goes through `APIRuntime.close()`.

The report itself is in [`openminion-runtime-failed-start-retention`](../openminion-runtime-failed-start-retention).

## Trees

| Name | Commit | What it is |
|---|---|---|
| `dev-042e0a2c` (`dev`) | `openminion/openminion@042e0a2c` | `dev` the PR's commits are on (it includes #458) |
| `pr-20034752` (`pr`) | `AliceMessi/openminion@20034752` | the PR branch head |

Each tree is installed from git into a fresh venv (`uv`, CPython 3.12) as `openminion[postgres] @ git+https://github.com/<repo>@<sha>`, and every probe runs with `python -I -X utf8` from an empty directory, with automatic garbage collection off from before `openminion` is imported.

## What ran

- Runner (GitHub-hosted Ubuntu 24.04.5, PostgreSQL 16 in Docker), [run 40683995519](https://github.com/AliceMessi/linux-runner/actions/runs/40683995519): [`probes/linux_setup.sh`](probes/linux_setup.sh) runs the report's probe and the PR probe on both trees, SQLite and PostgreSQL, one new database per PostgreSQL case. Outputs in [`linux/probes/`](linux/probes).
- Runner, [run 40685718215](https://github.com/AliceMessi/linux-runner/actions/runs/40685718215): [`probes/linux_repro.sh`](probes/linux_repro.sh) runs the report's minimal reproduction on both trees (SQLite and the unsupported memory provider). Outputs in [`linux/repro/`](linux/repro).
- Native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4: [`probes/run_probes_windows.sh`](probes/run_probes_windows.sh) runs the report's probe and the PR probe (SQLite cases) on both trees. The minimal reproduction ran with the same venvs, from an empty directory: `python -I -X utf8 repro_failed_start.py <WORK>/work-repro-<tree>`. Outputs in [`windows/<tree>/`](windows).
- The PR's tests: the repo gates and the focused, related and PostgreSQL tests on the branch head and on `dev` sources ([run 40683731206](https://github.com/AliceMessi/linux-runner/actions/runs/40683731206), [`linux/runner-gates.sh`](linux/runner-gates.sh)), the full `make test-ci` ([run 40683731659](https://github.com/AliceMessi/linux-runner/actions/runs/40683731659)), and the focused tests on native Windows ([`windows/pytest-windows-*.txt`](windows)).

## The report's minimal reproduction (unchanged, same sha256)

SQLite runtime storage and `OPENMINION_MEMORY_PROVIDER=not-a-provider`: the start raises `ConfigError` on both trees.

| | `dev`, after the exception | `pr`, after the exception | both, after `gc.collect()` |
|---|---|---|---|
| Linux | 16 open files under the root: 7 databases (`state.db` twice) | 0 | 0 |
| Windows | 15 open files: 7 databases; renaming `state.db`: `PermissionError [WinError 32]` | 0; renaming `state.db` works | 0 |

## The report's probe (unchanged, same sha256)

At the exception, automatic gc off:

| Case | `dev` | `pr` |
|---|---|---|
| `sqlite_memory_provider` (`ConfigError`) | 7 databases, 15 files; Windows: none of the 15 can be renamed | nothing open; `RuntimeStorageContext.close()` ran once |
| `sqlite_437_point` (the SQLite connection read made to raise) | 9 databases, 21 files; Windows: none can be renamed; after `gc.collect()` `artifact/index.db` still open | nothing open, also after `gc.collect()` |
| `postgres_runtime_migration` (`MigrationError`, existing `sessions` table) | 1 server connection, the emitter thread running, `telemetry.db` open; the same after `gc.collect()` | 0 connections, no emitter, nothing open |
| `postgres_437` | starts on both trees since #458; after `close()` nothing open, 0 connections | the same |
| controls (`sqlite_started_then_closed`, `postgres_storage_closed`) | nothing left after `close()`; the probe's own telemetry service, which the storage only borrows, stays open until the probe drops it | the same |

## The PR probe

[`probes/probe_447_boundaries.py`](probes/probe_447_boundaries.py) starts `APIRuntime` with one offline echo agent and makes one step fail. It counts each owner's close, and how often one object was closed, then records right after the exception: open files under the root, on Windows the files under the data root that cannot be renamed, the start threads still alive (`runtimectl-*`, `storage-pool-health-emitter`), and on PostgreSQL the other connections to the case's database.

At the exception (Linux SQLite / Windows SQLite / Linux PostgreSQL):

| Case (the step that fails) | `dev` | `pr` |
|---|---|---|
| none: start, then `close()` | nothing open / nothing open / 0 connections | the same |
| `memory-provider` (`ConfigError`, not injected) | 7 db / 7 db, 15 files locked / 1 connection, emitter, 6 db | nothing open on all three |
| `sessions-table` (`MigrationError` at v1, not injected) | 2 db / 2 db, 6 locked / 1 connection, emitter, 1 db | nothing open |
| `engine-emitter` (inside `StorageEngine.from_config`) | 2 db / 2 db, 6 locked / 1 connection, 1 db | nothing open |
| `storage-migration` | 2 db / 2 db, 6 locked / 1 connection, emitter, 1 db | nothing open |
| `extensions` | 2 db / 2 db, 6 locked / 1 connection, emitter, 1 db | nothing open |
| `ops` (after the action policy) | 3 db / 3 db, 9 locked / 1 connection, emitter, 2 db | nothing open |
| `support-authoring` (after the memory assembly started) | 8 db / 8 db, 18 locked / 1 connection, emitter, 7 db | nothing open |
| `after-support` | 9 db / 9 db, 21 locked / 1 connection, emitter, 8 db | nothing open |
| `gateway` (after the agent) | 9 db / 9 db, 21 locked / 1 connection, emitter, 8 db | nothing open |
| `connection` (the SQLite read in the constructor's arguments; SQLite only) | 9 db / 9 db, 21 locked / - | nothing open |
| `post-init` (`__post_init__`) | 9 db / 9 db, 21 locked / 1 connection, emitter, 8 db | nothing open |
| `runtime-manager` (after construction) | 9 db / 9 db, 21 locked / 1 connection, emitter, 8 db | nothing open |
| `bind` (after the manager started) | 9 db, sweeper / 9 db, sweeper, 21 locked / 1 connection, emitter, 8 db, sweeper | nothing open |
| `close-fails` (`connection`, and `RuntimeStorageContext.close` raises after closing; SQLite only) | 9 db / 9 db, 21 locked / - | nothing open |

In every case on both trees the caller got the original exception (the injected instance, or the `ConfigError` / `MigrationError`). On `dev` no counted close ran in a failing case. On `pr` each owner that had built something closed it, each object once. The exception is the LLM handle once the agent exists: `AgentService.close` and `close_runtime_components` both close it, as on a start that succeeds and is closed (the control). After `gc.collect()` the `pr` numbers are the same. On `dev` the injected cases stay open because the probe keeps the injected exception; the two uninjected SQLite cases are freed by the collection, the PostgreSQL ones are not.

## The PR's tests

- Runner, branch head (`20034752`): focused 22 passed; `-m postgres tests/integration/test_runtime_storage_postgres.py` 45 passed (5 new); related slice 2598 passed, 4 skipped, 1 xfailed, 1 failed (`test_unreachable_db_raises_with_redacted_url`, the 60 s timeout with the `postgres` extra installed, as on `dev`); the upstream `postgres-memory-retrieval` commands 19, 1, 7, 6, 45, 7 and 13 passed; `ruff check .`, `make format-check`, `make workflow-check`, `make lint` (mypy 4309 / 4309) and `make test-critical` (486 passed, 4 skipped) pass; `make test-ci` 20454 passed, 29 skipped, 1 xfailed, 1 failed (the same timeout).
- Runner, `dev` sources (`git checkout 042e0a2c -- src`): focused 21 failed, 1 passed (the success control); PostgreSQL 4 failed (the new failure cases), 41 passed.
- Windows, `tests/api/runtime/test_failed_start_cleanup.py tests/storage/test_failed_construction_cleanup.py tests/integration/test_runtime_storage_postgres.py` plus the existing runtime bootstrap tests: 47 passed, 46 skipped (no PostgreSQL server) on the head; 21 failed, 26 passed, 46 skipped on `dev` sources.

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes.

- `probes/`: `probe_failed_start_retention.py` and `repro_failed_start.py` are the report's, byte-identical (sha256 `6d556cd1...` and `da974dfc...`). `probe_447_boundaries.py`, `linux_setup.sh` and `linux_repro.sh` are byte-identical to what ran (the runner's own hashes are in `linux/probes/out/probe-sha256.txt` and `linux/repro/out/repro-sha256.txt`). In `run_probes_windows.sh` the line with the local work folder reads `W=<WORK>`.
- `linux/`: the gates and test-ci scripts and their logs (`gates-run-40683731206.txt`, `test-ci-run-40683731659.txt`); `probes/` and `repro/` with the run log and every output file (`boundaries-<tree>-<backend>-<case>.json` and its `.stderr.txt`, `boundaries-summary.txt`, the report probe's JSON and log, `pip-list-*`, `direct_url-*.json`, `os.txt`, `postgres-version.txt`).
- `windows/<tree>/`: the same outputs for the Windows runs, plus `repro-failed-start-sqlite.txt`; `windows/pytest-windows-*.txt` and `windows/windows-env-and-ruff.txt`.
- Local paths in the logs are replaced by `<WORK>`, `<REPO>`, `<DEV_REPO>`, `<VENV>` and `<USER_HOME>`.
