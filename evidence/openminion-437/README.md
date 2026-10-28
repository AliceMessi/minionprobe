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

## Final head

The PR is opened from `fix/437-postgres-runtime-storage` @ `de74deaf`: 3 commits on `dev` @ `988ac4ec` (the #436 merge), after the maintainer approved the scoped direction ([comment](https://github.com/openminion/openminion/issues/437#issuecomment-6459371829)). It has the same source change as `3ad2981c`. The test-stub change for `test_runtime_exposes_same_memory_adapter` is now in the fix commit, and the docs section lists only the paths the PostgreSQL tests check. Everything above was rerun on it, in `final-de74deaf/`:

| sha256 | file |
|---|---|
| `154ab6e2ac58d34d1601f893017b80d3489aeb983dee5f78d222ebdd9ebb4339` | `final-de74deaf/linux/gates-run-40629191323.txt` |
| `4a1e921ec9f1f8a7339fd808af52f0143ca8e7ea1d3b5e6dfad47e8eb647337f` | `final-de74deaf/linux/probes/probe_apiruntime_pg_dev.json` |
| `2d334abcb82afa786c567aa48cb5998eaa21e2548d6104bfa372c39982f37009` | `final-de74deaf/linux/probes/probe_apiruntime_pg_dev.log` |
| `8d178962a07773bc8868c21e34e00b0f08930b05f3755343195b0e89fe4c32f5` | `final-de74deaf/linux/probes/probe_apiruntime_pg_head.json` |
| `babd4a8b13cc194ae454d3f9dc88e34f814ce6ccc2189776ebcbffed701db3e2` | `final-de74deaf/linux/probes/probe_apiruntime_pg_head.log` |
| `434e9eb6c31012e260ed2b27dcb02563e2e17d906e4ce6fd7817067f385804b4` | `final-de74deaf/linux/probes/probe_runtime_postgres_data_dev.json` |
| `fb63d33ca5091a48a466ba6d0cc36b512840de3c940e3dccc353523a0801c781` | `final-de74deaf/linux/probes/probe_runtime_postgres_data_dev.log` |
| `7a4c4259012fa64c9f199655976bbc221e562ace5d13a0886b87dec1e124278a` | `final-de74deaf/linux/probes/probe_runtime_postgres_data_head.json` |
| `6678834af1f025a2dd57d00891cca2fbd02d86a13b2593f8c0718817b2df5a9c` | `final-de74deaf/linux/probes/probe_runtime_postgres_data_head.log` |
| `150f5daeb2b7b94646af21072fb19c5a8ad66a0dd1877f8fcad3ab1056834248` | `final-de74deaf/linux/probes/probes.sha256` |
| `576d959243c8c70c2c9d7307a79db3f2aaae02aafddc5d235f9493423a14a304` | `final-de74deaf/linux/probes/run-id.txt` |
| `cb88c9332c4a9c4631b823442da5c280fa262ef42be9c64c128ef2def8732225` | `final-de74deaf/linux/probes/spec_dev.txt` |
| `cbd19c9cef1e7bc71d469de67499a4192cc535b270cb04443bb49c83b408b2f2` | `final-de74deaf/linux/probes/spec_head.txt` |
| `e4e2e0165761d4375a6c00471f3b4d7f71fe985df9cfcfe1b20e28d5e81ba6c2` | `final-de74deaf/linux/runner-gates.sh` |
| `10cef90584e7635ee34a1761c23286372f46c93168f2c10445c1bfd950023625` | `final-de74deaf/linux/runner-test-ci.sh` |
| `6b20ea3bcfef767ab11d4aa417814364664961405380c18b579acac24d15761a` | `final-de74deaf/linux/test-ci-run-40629193948.txt` |
| `b14b657152459beec222802f8eff1771067a1d63c673c5e2904e36602a08bad9` | `final-de74deaf/runner_setup.sh` |
| `7fab10694da2b87343276ad3314e88ee28f04ddeec4a3245c45c1f995090490c` | `final-de74deaf/windows/pytest-dev-988ac4ec-sources.txt` |
| `a913a7f22c53199e12f29b33811762eee8a4b5ca81bdd11a7730d72394747e09` | `final-de74deaf/windows/pytest-head-de74deaf.txt` |

- `final-de74deaf/runner_setup.sh` is `probes/runner_setup.sh` with the two specs changed to `dev` @ `988ac4ec` and `de74deaf`. The probes are the files in `probes/` (same sha256, see `linux/probes/probes.sha256`).
- Probes ([run 40629230063](https://github.com/AliceMessi/linux-runner/actions/runs/40629230063)): the same results as the tables above, with `988ac4ec` in place of `691a1d54` and `de74deaf` in place of `3ad2981c`.
- Gates ([run 40629191323](https://github.com/AliceMessi/linux-runner/actions/runs/40629191323), script `final-de74deaf/linux/runner-gates.sh`):
  - focused tests: 17 passed on the head and on `988ac4ec` sources;
  - `-m postgres`: 47 passed on the head; on `988ac4ec` sources 3 failed (the three new tests), 44 passed;
  - the `test-ci`-marker slice: 1 failed (the PostgreSQL-extra timeout above), 1084 passed, 3 skipped, 1 xfailed, on both trees;
  - `ruff check .`, `make format-check` (5131 files), `make workflow-check`, `make lint` (mypy ratchet 4309 / 4309) and `make test-critical` (486 passed, 4 skipped) pass.
- `make test-ci` ([run 40629193948](https://github.com/AliceMessi/linux-runner/actions/runs/40629193948)): 1 failed (the same timeout), 20417 passed, 29 skipped, 1 xfailed.
- Windows: 17 passed, 40 skipped on the head and on `988ac4ec` sources, in a fresh `uv` venv.
