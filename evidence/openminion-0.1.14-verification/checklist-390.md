# #390 on the installed 0.1.14: the maintainer's checklist, item by item

The maintainer closed #390 on 28 Oct 2026 after his own run on macOS with SQLite ([comment 6473402104](https://github.com/openminion/openminion/issues/390#issuecomment-6473402104)). This is our full result for each item of his checklist, on native Windows and on Linux with PostgreSQL.

Release verification on the installed 0.1.14: **PASS**. A runtime memory assembly build that fails after it obtained the artifact store, the memory store, the audited store or the service closes each resource it owns at that point exactly once, leaves the caller's scheduler and vector adapter alone, and the caller still gets the build's own exception when a close raises. On native Windows, with cyclic GC off and no `gc.collect`, every SQLite file of a failed build can be renamed and deleted right away. On PostgreSQL the owned engine is disposed once and no server connection is left. The probe passed 82/82 on native Windows and 115/115 on Linux (the same 82, plus 33 PostgreSQL checks on a live PostgreSQL 16.15 server). On the published 0.1.13, 56 and 58 of those checks fail.

- **Verify from a clean installed package** **PASS**
  - Windows: `openminion==0.1.14` from PyPI in a new venv. The probe prints `openminion.__version__` `0.1.14` and `openminion.__file__` `<VENV>\Lib\site-packages\openminion\__init__.py`. Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4.
  - Linux: `openminion[postgres]==0.1.14` from PyPI in a new venv on a GitHub-hosted runner with no openminion checkout and no repo tests. The probe prints `0.1.14` and `<VENV>/lib/python3.12/site-packages/openminion/__init__.py`. Ubuntu 24.04.5 LTS, CPython 3.12.15, SQLite 3.45.1, `PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) on x86_64-pc-linux-gnu` (`postgres:16`), SQLAlchemy 2.1.6, psycopg 3.3.6.
  - The installed builder module, `openminion/services/runtime/memory.py`, has the same sha256 on both machines (`6c5eea49d441e686313ff94a8ee528f6739c36e48797d17b8d678163a6fb2e34`) and defines `_MemoryBuildOwners`. 0.1.13's module (`36aba10e...`) does not.
  - The probe runs with `python -I -X utf8` from an empty directory outside any checkout. It calls the installed `build_memory_v2_runtime_assembly` in process, because the checklist needs GC control and close counters, and uses only its public parameters. The artifact store is the package default (`create_default_artifactctl`), with its SQLite index under the case's `OPENMINION_DATA_ROOT`.
  - To count closes, the probe wraps `__init__` and `close()` of the package classes the build obtains: `ArtifactCtl`, `SQLiteMemoryStore`, `InMemoryMemoryStore`, `RemoteMemoryStore`, `PostgresMemoryStore`, `SQLiteMemoryAuditSink`, `AuditedMemoryStore`, `MemoryService` and `BackendMemoryStoreAdapter`. The wrappers call through and hold each instance by weak reference only, so the probe keeps nothing alive.
  - Each check compares the close calls of every instance created in the case with the expected map. A second close, or a close of something the build did not own, fails the check.
- **failure after artifact/store/audit/service acquisition closes each currently owned resource exactly once** **PASS**. Each case ran in its own folder, on Windows and Linux:

  | Failure point | How the build fails | Owned at that point | close() calls |
  |---|---|---|---|
  | after the artifact store | a remote store with no endpoint: `InvalidArgumentError: remote backend requires remote.endpoint` (real); an external provider whose adapter is not registered: `InvalidArgumentError: No external backend registered for 'probe-not-registered'` (real) | `ArtifactCtl` | 1 |
  | after the memory store | the memory root is a file, so the audit sink cannot be created: `FileExistsError` (real); the builder's `SQLiteMemoryAuditSink` replaced by a raising callable, as upstream's test does | `ArtifactCtl`, `SQLiteMemoryStore` | 1 each |
  | after the audited store | the builder's next call, `memory_runtime_configuration.merged_ranking_config`, replaced by a raising callable | `ArtifactCtl`, `AuditedMemoryStore` with its `SQLiteMemoryStore` and `SQLiteMemoryAuditSink` | 1 each |
  | after the service | `runtime.memory_retrieval_max_chars = "not-a-number"`: `ValueError` while the gateway is built (real); `adapter_cls` (a parameter of the builder) raising, with the sqlite, mock and remote stores | `ArtifactCtl`, the owning `MemoryService` with its audited store, store and sink | 1 each |

  - After the service, the memory store (`SQLiteMemoryStore`, `InMemoryMemoryStore` or `RemoteMemoryStore`) is closed once, through the service. It is not closed again directly.
  - The sophiagraph backend creates a second, non-owning `MemoryService` for portability. It is not closed (0 calls).
  - Nothing else the probe tracks was created or closed.
- **leaves borrowed scheduler/vector resources usable** **PASS**
  - Setup: the caller passes the package's `VectorSyncScheduler` and a `VectorIndexAdapter` over the package's `SQLiteVecBackend` (`caller/vectors.db`), with a deterministic 8-dimension hash embedding provider written for the probe.
    - The package's `VectorIndexAdapter` has no `close()`, so the probe's caller-owned subclass adds one: it closes the backend's connection. A close attempt from the build would therefore be counted.
  - Failure: `adapter_cls` raises after the service exists.
  - The build's own resources are closed once each, as above. The scheduler's `stop()` and the adapter's `close()` were called 0 times.
  - After the failure:
    - the `vectors.db` connection still answers a query;
    - a vector indexed through the adapter is found by its search;
    - the scheduler was never started and was not stopped.
  - The same two objects then go to a new, successful assembly:
    - `assembly.start()` starts the scheduler thread;
    - a record the service writes is synced (`records_processed` 1) and is the top search hit;
    - `assembly.close()` stops the scheduler (one `stop()`, the thread ends) and closes the assembly's own resources once.
  - Afterwards the caller closes its adapter and deletes `vectors.db` at once.
- **preserves the original exception if cleanup fails** **PASS**
  - Setup: `adapter_cls` raises a `GatewayUnavailable` instance. Then, after doing its real close:
    - the owning `MemoryService.close()` raises `OSError`;
    - or `ArtifactCtl.close()` raises `OSError`;
    - or both raise.
  - In all three cases the caller gets the same exception object:
    - its identity is checked;
    - its type and message are unchanged;
    - the innermost traceback frame is the failing `adapter_cls`;
    - `__context__` and `__cause__` are `None`.
  - The close that raised did not stop the other close: every owned resource was still closed once.
  - On PostgreSQL, with `ArtifactCtl.close()` raising, the caller also got the same object, and the engine was still disposed once with no server connection left.
- **and permits immediate SQLite file operations/native-Windows checks** **PASS**
  - Setup: native Windows. The probe calls `gc.disable()` before it imports openminion and replaces `gc.collect` with a counter that does not collect; the counter stayed at 0.
  - Right after each of the 14 failures, still in the `except` block with the exception and its traceback alive:
    - no sqlite3 connection is open on any file of the case (`gc.get_objects()`, which does not collect, and `PRAGMA database_list`);
    - every SQLite file renames away and back and deletes: `data/artifact/index.db` in every case, plus `memory/memory.db`, `store/memory.db` or `external/external.sqlite3` where the case created one;
    - the case folders are removed. The exception is the caller's own `vectors.db` folder in the borrowed case, which the caller still holds open by design.
  - After the `except` block, still nothing is open and no file is left.
  - The file that matters is the artifact index. It holds a persistent WAL connection from `ArtifactCtl`. `SQLiteMemoryStore` opens a connection per operation, and the audit sink connects only on its first write, so a failed build never holds `memory.db` or `memory.audit.db` open, on either version.
  - Linux gives the same results. File operations succeed there even on open files, so the open-connection checks are what count on Linux.
- **PostgreSQL should dispose the owned engine/connection.** **PASS**. On the runner, the build failed after the store (sink injection), after the audited store (ranking injection) and after the service (`adapter_cls`), plus the failing-close case above. Each case used its own schema and its own `application_name`.
  - At the failure, read from inside the failing step, the owned `PostgresMemoryStore` held 1 server connection (`pg_stat_activity`) and its engine had not been disposed.
  - The store had created `memory_records` in its own schema on the server.
  - Afterwards, in each case:
    - `PostgresMemoryStore.close()` was called once, along with the other resources owned at that point;
    - the engine's `engine_disposed` event fired exactly once;
    - `pg_stat_activity` showed 0 connections for the case's `application_name`, on the first poll inside the `except` block and again afterwards.
  - The artifact index had no open connection either.
- **Keep successful assembly lifetime and external-backend controls.** **PASS**
  - Successful SQLite assembly:
    - The build closes nothing. `assembly.artifactctl` and `assembly.service` are the instances the build obtained, and a record write produces the `memory.record.put` audit event.
    - While the assembly lives, `index.db` and `memory.audit.db` are open.
    - `assembly.close()` closes each resource once. A second close returns the same result object and closes nothing more.
    - Then nothing is open, and on Windows the files rename and delete at once.
  - Successful PostgreSQL assembly:
    - while it lives it holds 1 server connection and nothing has been disposed;
    - a record it writes is read back on the server (1 row);
    - `assembly.close()` closes each resource once and disposes the engine once, leaving 0 connections. The second close returns the same result.
  - Successful external-backend assembly: the probe registered an adapter over the package's reference sophiagraph SQLite backend, with a `close()` added.
    - No `SQLiteMemoryStore`, audit sink or audited store is created. The service wraps a `BackendMemoryStoreAdapter`, `delegated_store` is `None`, and a record write works.
    - `assembly.close()` closes the backend, the adapter, the service and the artifact store once each. A second close changes nothing.
  - Failed external builds:
    - With an unregistered adapter, only the artifact store is closed (once).
    - With a registered adapter and `adapter_cls` failing, the external backend is closed once through the owning service and `BackendMemoryStoreAdapter`, and the artifact store once. No built-in store or sink is created.
- **Include runnable probe, actual package path/version and results; no release closure based on this merge alone.** **PASS**
  - The probe, its helper module, the runner steps and the Windows script are linked below.
  - The JSON outputs have one check per claim, with observed and expected values. The logs have the console output.
  - The package path and version are above.
  - Everything here ran on the installed `openminion==0.1.14` from PyPI, without the repo's tests or a source checkout. This comment reports the verification only and does not close anything.

As a control, I ran the same probe on the published 0.1.13: `openminion==0.1.13` on Windows and `openminion[postgres]==0.1.13` on the same runner. Its builder has no `_MemoryBuildOwners`. 56 of 82 checks fail on Windows and 58 of 115 on Linux:
- In all 14 failure cases nothing is closed. Every close count is 0: the artifact store, the memory store, the audit sink, the audited store, the service and the external backend.
- The artifact index connection (`data/artifact/index.db`, opened in `ArtifactCtl.__init__`) is still open in the `except` block. With GC off it is also still open after the block.
- On native Windows, renaming or deleting `index.db`, its `-wal` and `-shm` files, and removing the `data` folder all fail with `PermissionError [WinError 32]`. `memory.db`, which has no open connection, renames and deletes fine. On Linux those operations succeed, and the open connection is what fails.
- On PostgreSQL, `engine_disposed` never fires. The store's server connection is still in `pg_stat_activity` after 6 seconds of polling, both in the `except` block and after it.
- What passes on 0.1.13:
  - the same errors reach the caller (with no cleanup, no failing close can get in the way);
  - the caller's scheduler and vector adapter are untouched and keep working;
  - the success controls and the external-backend success control.

**Package:** `openminion==0.1.14` from PyPI in fresh venvs (uv). The wheel `openminion-0.1.14-py3-none-any.whl` has sha256 `efd06138b2f10da019954539f5e33749aea4cd81727436d659efe0821345f3c4`, which matches PyPI's digest, and all 2744 hashed RECORD entries match the installed files on both machines. Native Windows 11 Pro 10.0.22631 with CPython 3.12.11 and SQLite 3.50.4; an Ubuntu 24.04.5 GitHub-hosted runner with no openminion checkout, CPython 3.12.15 and SQLite 3.45.1, with PostgreSQL 16.15 (`postgres:16`), SQLAlchemy 2.1.6 and psycopg 3.3.6. Release source: the tag `v0.1.14` (annotated tag `a029066d`) points to commit `6f2c8f45`. The #432 merge `68d623d1` and its reviewed head `e9203bdd` are both ancestors of it (`git merge-base --is-ancestor`, and `behind_by` 0 in the GitHub compare API), and every file in the PyPI wheel and sdist that comes from the repository is byte-identical to the tag. The control used `openminion==0.1.13` from PyPI in a separate venv, whose wheel also matches PyPI's digest (`266033a0...`). Every probe prints `openminion.__file__` and `__version__`, runs with `python -I` from an empty directory outside any checkout, and uses no repo test fixtures.

```
# Windows, 0.1.14 and the 0.1.13 control (probes/run_390_windows.sh):
cd <empty dir>
<venv>/Scripts/python.exe -I -X utf8 probes/probe_390.py <work> windows/390/probe_390.json
<venv0113>/Scripts/python.exe -I -X utf8 probes/probe_390.py <work>-0113 windows/390/control-0.1.13/probe_390.json
# Linux runner, openminion[postgres]==0.1.14 and ==0.1.13 from PyPI, postgres:16 (probes/linux_setup.sh + probes/steps_390.sh):
<venv>/bin/python -I -X utf8 probes/probe_390.py <work> probe_390.json --postgres     # needs OPENMINION_TEST_POSTGRES_URL
<venv0113>/bin/python -I -X utf8 probes/probe_390.py <work>-0113 probe_390_0113.json --postgres
```

Evidence:
- Windows (0.1.14): https://github.com/AliceMessi/minionprobe/tree/main/evidence/openminion-0.1.14-verification/windows/390
- Windows control (0.1.13): https://github.com/AliceMessi/minionprobe/tree/main/evidence/openminion-0.1.14-verification/windows/390/control-0.1.13
- Linux with PostgreSQL, both versions (runner run [40665681581](https://github.com/AliceMessi/linux-runner/actions/runs/40665681581); the 0.1.13 control is in `probe_390_0113.*`): https://github.com/AliceMessi/minionprobe/tree/main/evidence/openminion-0.1.14-verification/linux/390
- Probes (`probe_390.py`, `probe_390_lib.py`, `steps_390.sh`, `run_390_windows.sh`): https://github.com/AliceMessi/minionprobe/tree/main/evidence/openminion-0.1.14-verification/probes
