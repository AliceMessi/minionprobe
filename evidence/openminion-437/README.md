# OpenMinion #437: `APIRuntime` on PostgreSQL runtime storage, the fix

Validation of the PR for [openminion#437](https://github.com/openminion/openminion/issues/437), as proposed there ([comment](https://github.com/openminion/openminion/issues/437#issuecomment-6446732529)). Branch `fix/437-apiruntime-postgres-storage` @ `3ad2981c` (4 commits on `dev` @ `691a1d54`).

`finalize_runtime_instance` read `runtime_storage.connection` for every backend. That property raises for a record store without a SQLite connection, so `APIRuntime` did not start with `storage.backend: postgres`. The fix reads it only when `base_config.storage.record_backend()` is `record.sqlite`, the selector that built the runtime storage. For other backends `APIRuntime.storage_connection`, and the compatibility wrapper's `OpenMinionRuntime.storage`, are `None`.

The startup report itself is in [`openminion-apiruntime-postgres-startup`](../openminion-apiruntime-postgres-startup).

## Files

| sha256 | file |
|---|---|
| `f67b46decf33bb616515fa335d1e01467a16c6e7e663158ed8a7caeab8a35cf4` | `probes/probe_apiruntime_pg.py` |
| `2aa63ed799f77b0798413734cd78e0a9a4a43e86c33c7fb1b842553d73b8f234` | `probes/probe_runtime_postgres_data.py` |
| `5642824c5638c61019d71bda04197d7bd375cc791172cf4d82ebc57cda587c7f` | `probes/runner_setup.sh` |

- `probes/probe_apiruntime_pg.py` is the probe of the startup report, unchanged (same sha256). It starts `APIRuntime` with the storage section, with the `OPENMINION_STORAGE_*` variables and with a `postgresql://` URL, and runs `openminion ... room create` and `sessions list --json`. After each PostgreSQL case it counts the connections the probe process still holds to that database after `gc.collect()`.
- `probes/probe_runtime_postgres_data.py` checks where a started runtime keeps its data. On SQLite and on PostgreSQL it runs:
  - `APIRuntime.from_config_path`, one echo-agent turn through `Agent(runtime=...)`, then `close()`;
  - `OpenMinionRuntime.from_config_path`, then `close()`;
  - `openminion ... room create` with the installed console script.

  It records the server connections while the runtime is open and after `close()`, the session, message and participant rows in PostgreSQL, and the SQLite files under the runtime home that have a `sessions` table. It is a cleaned-up version of the second prototype probe ([run 40500183708](https://github.com/AliceMessi/linux-runner/actions/runs/40500183708)).
- `probes/runner_setup.sh` installs `dev` @ `691a1d54` and the branch head @ `3ad2981c` from git into fresh venvs (`uv venv --python 3.12`, `openminion[postgres] @ git+...`). It runs both probes with `python -I` from an empty directory on each.
- Every PostgreSQL case uses a new, empty database and a hand-written config file with one offline `echo` agent. Paths are shortened to `<VENV>` and `<RUNNER_TEMP>`, and the PostgreSQL password is written as `***`.

Runs on AliceMessi/linux-runner (GitHub-hosted Ubuntu 24.04.5, PostgreSQL 16.15 in Docker):

- `linux/probes/`: [run 40541883595](https://github.com/AliceMessi/linux-runner/actions/runs/40541883595), the two probes on both trees. Each has a `.log` (the `CASE` lines plus the package's own log output) and a `.json` (every case in full). `spec_*.txt` are the installed specs, and `probes.sha256` the hashes of the shipped probes.
- `linux/gates-run-40541883618.txt`: [run 40541883618](https://github.com/AliceMessi/linux-runner/actions/runs/40541883618), with the script `linux/runner-gates.sh`. CPython 3.11.17, SQLite 3.45.1, the `postgres` extra and a PostgreSQL service. On the branch head it ran:
  - the focused tests, a broader slice and the PostgreSQL tests;
  - the same tests again after `git checkout 691a1d54 -- src`;
  - back on the head: `ruff check .`, `make format-check`, `make workflow-check`, `make lint` and `make test-critical`.
- `linux/test-ci-run-40541883590.txt`: `make test-ci` on the branch head ([run 40541883590](https://github.com/AliceMessi/linux-runner/actions/runs/40541883590)), with the script `linux/runner-test-ci.sh`.
- `windows/`: the new SQLite tests and the touched existing tests on native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4. They ran on the worktree at `3ad2981c` and on a `git archive` of `691a1d54`'s `src` with the same tests. The PostgreSQL tests skip there (no server).

Timestamped log lines from the package are left out of the gates and test-ci logs.

## Results

`probe_apiruntime_pg.py` (installed packages):

| Case | `dev` @ `691a1d54` | `3ad2981c` |
|---|---|---|
| `APIRuntime`, SQLite (control) | starts, `storage_connection` is a `Connection` | the same |
| `APIRuntime`, PostgreSQL storage section | `RuntimeError: runtime record_store does not expose sqlite connection`; 1 connection left after `gc.collect()` | starts, `RecordStorePostgres`, `storage_connection` `None`; 0 connections after `close()` |
| the same, via `OPENMINION_STORAGE_BACKEND` and `OPENMINION_STORAGE_POSTGRES_URL` | the same `RuntimeError`; 1 connection left | starts; 0 connections after `close()` |
| the same, `postgresql://` URL | the same `RuntimeError`; 1 connection left | starts; 0 connections after `close()` |
| `build_runtime_storage` directly, then `.connection` | raises the same `RuntimeError` | the same (the property is unchanged) |
| `openminion ... room create`, PostgreSQL | exit 1, `openminion room: startup error — runtime record_store does not expose sqlite connection` | exit 0 |
| `openminion ... sessions list --json`, PostgreSQL | exit 0 | exit 0 |
| `storage migrate --backend postgres` first, then `APIRuntime` | `MigrationError`, `relation "sessions" already exists` | the same (#446) |

`probe_runtime_postgres_data.py` (installed packages):

| Case | `dev` @ `691a1d54` | `3ad2981c` |
|---|---|---|
| `APIRuntime` and one turn, SQLite (control) | turn `completed`; sessions in `state.db` (1) and `brain/sessions.db` (1) | the same |
| `APIRuntime` and one turn, PostgreSQL | start fails with the `RuntimeError`; 1 connection left | turn `completed`; PostgreSQL has 1 session and 2 messages; 1 connection while open, 0 after `close()`; no `state.db` |
| wrapper, SQLite (control) | `storage` is a `Connection`, the runtime's `storage_connection` | the same |
| wrapper, PostgreSQL | start fails; 1 connection left | `storage` `None`, `sess` `SessionStore`, `RecordStorePostgres`; 0 connections after `close()` |
| `room create`, SQLite (control) | exit 0, `participants=1`; the room is in `state.db` | the same |
| `room create`, PostgreSQL | exit 1, startup error | exit 0, `participants=1`; the room and its agent owner row are in PostgreSQL; 0 connections after; no SQLite file has a `sessions` table |

In the PostgreSQL turn, `brain/sessions.db` still gets its session row. It is a module store under the data root, which `storage.backend` does not select, on both trees.

Tests (gates run 40541883618):

- `pytest -q tests/api/test_runtime_storage_connection.py tests/api/runtime/test_memory.py tests/test_room_cli.py` -> 17 passed; on `691a1d54` sources also 17 passed (these are SQLite controls and the existing tests).
- `pytest -q -m postgres tests/integration/test_runtime_storage_postgres.py tests/integration/test_runtime_memory_postgres.py` -> 47 passed. On `691a1d54` sources: 3 failed, 44 passed. The three are the new PostgreSQL tests: two `RuntimeError: runtime record_store does not expose sqlite connection` and the room test's `assert 1 == 0` (exit 1).
- `pytest -q -m "<test-ci markers>" tests/api tests/services/runtime tests/storage tests/cli/test_plugins_cli.py` -> 1 failed, 1083 passed, 3 skipped, on both trees. The failure is `tests/storage/test_startup_validation.py::TestValidatePostgresConnectionFailure::test_unreachable_db_raises_with_redacted_url`, a 60 s pytest timeout. With the `postgres` extra installed it times out on `dev` too.
- `ruff check .`, `make format-check` (5131 files), `make workflow-check`, `make lint` (mypy ratchet 4309 / 4309) and `make test-critical` (486 passed, 4 skipped) pass.
- `make test-ci` (run 40541883590) -> 1 failed (the same timeout), 20412 passed, 29 skipped.

An earlier gates run on `50e0d77a` ([run 40537465219](https://github.com/AliceMessi/linux-runner/actions/runs/40537465219)) found one more failure, `tests/api/runtime/test_memory.py::test_runtime_exposes_same_memory_adapter`. Its config stub had no `storage` section. Commit `3ad2981c` gives that stub the default `StorageConfig()`, and the runs above are on it.

Windows: `pytest -q tests/api/test_runtime_storage_connection.py tests/api/runtime/test_memory.py tests/test_room_cli.py tests/integration/test_runtime_storage_postgres.py` -> 17 passed, 40 skipped, on both trees.
