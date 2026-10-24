# Release verification of openminion 0.1.11 (installed package)

This folder covers the installed-package checks the maintainer asked for on openminion/openminion #352, #353, #361, #365, #368, #373 and #378. These issues are labelled `status:awaiting-release`, and their fixes (PRs #372, #363, #366, #370, #380, #377 and #379) are in 0.1.11, which was published on GitHub and PyPI on 24 Oct 2026.

Everything below ran against `openminion==0.1.11` installed from PyPI into fresh virtual environments, not against a source checkout. The probes are standalone scripts that use only the installed package; the wheel does not ship the repo's `tests/` fixtures. The SQLite and native Windows cases ran on native Windows 11. The PostgreSQL cases, plus Linux parity runs, ran on a GitHub-hosted Ubuntu runner that installed the same wheel from PyPI and had no openminion checkout. The upgrade cases created their starting databases with the published `openminion==0.1.10`. Each checklist is the latest maintainer comment on its issue that lists what to verify before closure. Everything ran on 24 Oct 2026.

## Package provenance

| | |
|---|---|
| Install (Windows) | `uv venv --python 3.12.11 venv` and `uv pip install --python venv/Scripts/python.exe --index-url https://pypi.org/simple --refresh "openminion==0.1.11"` into a new venv |
| Install (Linux) | [`probes/linux_setup.sh`](probes/linux_setup.sh): `uv venv --python 3.12` and `uv pip install --index-url https://pypi.org/simple "openminion[postgres]==0.1.11"` (plus `openminion[postgres]==0.1.10` in a second venv for the upgrade cases) |
| Wheel | `openminion-0.1.11-py3-none-any.whl`, uploaded to PyPI 2026-10-24T11:07:37Z |
| sha256 (PyPI JSON) | `90bbe2bc1fe0f57bc74dbe478c96411006ccc2429295e12fe0b05fd79db6e29b` |
| sha256 (downloaded wheel) | `90bbe2bc1fe0f57bc74dbe478c96411006ccc2429295e12fe0b05fd79db6e29b` (match, on both machines) |
| Installed files | All 2743 hashed RECORD entries of the wheel are in the installed RECORD and match the installed files' sha256: 0 mismatched, 0 missing (Windows and Linux) |
| `pip show` (`uv pip show`) | `Name: openminion`, `Version: 0.1.11`, `Location: <VENV>\Lib\site-packages` (Windows), `<VENV>/lib/python3.12/site-packages` (Linux) |
| Imported module | Every probe prints `openminion.__file__` = `<VENV>\Lib\site-packages\openminion\__init__.py` (Linux: `<VENV>/lib/python3.12/site-packages/...`) and `__version__` = `0.1.11`. Probes run from empty directories outside any checkout, with `python -I` |
| Windows | Native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4 |
| Linux | Ubuntu 24.04.5 LTS GitHub-hosted runner ([AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner), preset `none`: the checkout is the runner repo itself, not openminion), CPython 3.12.15, SQLite 3.45.1, PostgreSQL 16.15 (`postgres:16`), SQLAlchemy 2.1.6, psycopg 3.3.6 |
| GitHub release | `v0.1.11`, published 2026-10-24T10:35:12Z. The annotated tag `aa38948c` points to commit `ad391ac2` ("Merge pull request #382 from openminion/dev"). All seven merge commits (#363 `455c8a9a`, #366 `3bf7138a`, #370 `cf61d37c`, #372 `b7ed475d`, #377 `a306f9f4`, #379 `2b68edc8`, #380 `ba0a4f0d`) are ancestors of it (GitHub compare API: `behind_by` 0). This was read from the GitHub API; I did not rebuild the wheel from that commit |
| 0.1.10 (seed and control) | `openminion==0.1.10` from PyPI in a separate venv: wheel sha256 `c8f093890f1715d20af5a674d6c5d925438492cca40f87a98a26e5140a154a09` matches PyPI, and all 2742 RECORD entries match |
| Upgrade note followed | 0.1.11 CHANGELOG: "Runtime migration 13 requires every process sharing a database to upgrade together. Stop all writers and retain a pre-upgrade backup; mixed-version writers and downgrade-in-place are not supported." |

Details are in [`provenance.json`](provenance.json), [`probes/provenance.py`](probes/provenance.py) and [`linux/provenance/`](linux/provenance) (provenance-only runner run 40096190597). Every runner run repeats the check in its own `provenance-linux.json`, and its `probe-sha256.txt` shows that the probes it ran are byte-identical to the files in `probes/`.

## Results

Every check below ran on the published 0.1.11, and nothing failed on it. A few sub-cases were not exercised, and their sections name them: the delegated read on the remote memory backend in #368, which has no served endpoint, and the unsupported mixed-version writing and downgrade paths in #352.

| Issue | Maintainer's acceptance check | Result | Evidence |
|---|---|---|---|
| #352 | Clean install; migration-13 upgrade contract followed; a >2,000-event migration-12 history with a long run spanning the window and interleaved runs/threads upgraded; fresh and upgraded databases report correct runs, run events, state/step/error/start/end/count, invocation and thread high water, raw payloads unchanged; exact/padded/token/NaN/Infinity/NUL/long ids on both backends, decoded-ID order, bounded rows; replay/conflict, terminal/capture, rollback, deletion keep events and facts consistent; a backfill failure leaves 13 unapplied and history unchanged, rerun after repair | **PASS** (70/70 Windows SQLite; 72/72 Linux SQLite; 72/72 PostgreSQL 16; runner run 40097780930) | [`windows/probe_352_*`](windows), [`linux/352/`](linux/352), [`probes/steps_352.sh`](probes/steps_352.sh) |
| #353 | Equal-timestamp append/read order, newest limits, filters and cursors; compaction then more tied appends; upgrade from the prior schema and restart without hiding uncompacted messages (SQLite and PostgreSQL); transcript message order and the native Windows reproduction | **PASS** (fresh store: 36/36 Windows SQLite, 36/36 Linux SQLite, 32/32 PostgreSQL; upgrade: 23/23 on each; Windows real clock: 0/71 tied sessions out of order, against 42/66 on 0.1.10) | [`windows/probe_353.json`](windows/probe_353.json), [`windows/probe_353_upgrade.json`](windows/probe_353_upgrade.json), [`windows/probe_353_issue-v0111.json`](windows/probe_353_issue-v0111.json), [`linux/353/`](linux/353) |
| #361 | Normal SDK shutdown: the audited turn opens the runtime-created sink, shutdown closes it exactly once, repeated close is safe, borrowed sinks stay open, a still-running scheduler prevents closure; native Windows cleanup of the audit database | **PASS** (18/18 + 8/8 on Windows; 18/18 + 8/8 on Linux) | [`windows/probe_361.json`](windows/probe_361.json), [`windows/probe_361_cleanup.json`](windows/probe_361_cleanup.json), [`linux/361/`](linux/361) |
| #365 | Twelve tied events export in insertion order across IDs 9/10 (JSONL, Markdown); at exact mixed ties, messages in append order then events in insertion order; primary timestamp order and #363 message order intact | **PASS** (15/15 Windows, 15/15 Linux; the 0.1.10 control exports `1,10,11,12,2..9`) | [`windows/probe_365.json`](windows/probe_365.json), [`linux/365/probe_365.json`](linux/365/probe_365.json), [`windows/probe_365-v0110-control.json`](windows/probe_365-v0110-control.json) |
| #368 | PostgreSQL SDK turn opens a real connection and repeated `runtime.close()` disposes the owned engine once with no connection left; borrowed audited PostgreSQL store keeps its engine; SQLite/mock/remote close cleanly with owned vs injected artifact resources and no second closer for delegated reads; scheduler boundary | **PASS** (PostgreSQL 8/8 on Linux; SQLite/mock/remote 21/21 on Windows and 21/21 on Linux) | [`linux/368/probe_368_pg.json`](linux/368/probe_368_pg.json), [`windows/probe_368.json`](windows/probe_368.json), [`linux/368/probe_368.json`](linux/368/probe_368.json) |
| #373 | With cyclic GC disabled, migration/detection/verification connections close without `gc.collect()`; metadata writes commit; failing metadata/verification hooks propagate, roll back and close only owned connections; on native Windows the authored-tool DB and runtime dir can be renamed/deleted right after an SDK turn | **PASS** (26/26 Windows; 26/26 Linux; control on 0.1.10: 13/26 fail) | [`windows/probe_373.json`](windows/probe_373.json), [`windows/control-0.1.10/probe_373.json`](windows/control-0.1.10/probe_373.json), [`linux/373/`](linux/373) |
| #378 | A real OMX export/import preserves rows and closes every connection the import opens, on success and on a forced mid-import failure; failed writes roll back and keep the error; on native Windows with GC disabled the target can be renamed/deleted right after return; borrowed record stores stay with their owner | **PASS** (19/19 Windows; 19/19 Linux; control on 0.1.10: 6/19 fail) | [`windows/probe_378.json`](windows/probe_378.json), [`windows/control-0.1.10/probe_378.json`](windows/control-0.1.10/probe_378.json), [`linux/378/`](linux/378) |

### #352 (`probes/probe_352_lib.py`, `probe_352_history.py`, `probe_352_check.py`, `probe_352_contract.py`, `probe_352_diff.py`, `probe_352_compare.py`, `steps_352.sh`)

The same sequence ran on native Windows (SQLite) and on the Linux runner (SQLite, then PostgreSQL 16), with the published 0.1.10 and 0.1.11 wheels. The oracle is a plain event-by-event reduction over every stored event, with the per-event normalization copied from 0.1.10's `task/run/status.py`, not imported from 0.1.11. Raw rows are read through the database driver, not through the store under test.
- **Upgrade, following the CHANGELOG note.** 0.1.10 (runtime schema head 11: it has neither #363's migration 12 nor #372's migration 13) writes a 5,192-event history through its public storage API, and the writer is closed. The history has two sessions: the issue's probe unchanged (2,000 events of run `old`, then `new` queued/failed), and a 3,190-event session with 2,757 run events: run `long` starts at the session's first event and is still running at the end, run `long2` starts early and completes at the end, interleaved runs on three threads, tool/response/attach events, padded run ids and states, JSON-token text, and NaN/Infinity/NUL in run fields and in unrelated fields, on a non-monotonic clock. A backup is taken before anything is migrated: a file copy (SQLite) or `pg_dump` of the schema (PostgreSQL, 1,117,316 bytes, recorded in [`linux/352/backups.log`](linux/352/backups.log)). No 0.1.10 binary is run against a migrated database, and mixed-version writing and downgrade are not exercised, as they are unsupported.
- **Migration-12 database.** One copy gets only 0.1.11's migrations up to 12 from the package's own runner (`migrate_database` / `migrate_record_store`): ledger 1..12, no `event_run_facts`, events unchanged. The normal open (`build_runtime_storage`) then applies 13. A second copy goes 11 -> 13 in one normal open. In both, every stored event row has the same sha256 before and after, `event_run_facts` has one row per event, equal to 0.1.11's `event_run_facts()` and to the 0.1.10 expressions, and ledger is 1..13.
- **Readers, fresh and upgraded.** On the two upgraded databases and on a fresh database where 0.1.11 wrote the same history:
  - `list_session_runs` equals the reduction for 10-11 scan limits (1 up to above the session's run events, including 1,999/2,000/2,001) x limits 1/3/20/500, and with defaults;
  - `list_session_run_events` equals each run's newest events for all 974 runs, limits 1/2/200/1000, exact and padded requests (7,792 calls);
  - `resolve_thread_lifecycle` equals the reduction for 12 conversation/thread requests (latest run and state, invocation id and source event, latest event id = the thread's high water, `updated_at`, delivery, writer attach);
  - the issue's probe reports runs `[new failed, old completed]`, 2 events for `new`, and the projection `new` / `failed` / `new-inv` at event 2002 = the high water;
  - `long` is listed with `started_at` = the session's first event (id 2003, below the default window, which starts at id 2882), all 751 events, `running`, step `final-step`, no `ended_at`; `long2` with its first start, last end and 242 events;
  - the reader outputs (session ids removed) are identical across the fresh and the two upgraded databases.
- **Roll-back material.** The pre-upgrade backup read back with 0.1.10 has ledger 11 and the same sha256 of every event row as the 0.1.10 writer recorded. On PostgreSQL the `pg_dump` was restored into a new database first.
- **Contract cases** (`probe_352_contract.py`, adapted from the PR's shared scenario file, compared with the oracle): interleaved runs with special/padded/nested/upper-case ids; interleaved threads; padded fields, null/bool/number/container ids, JSON-token text, zero-width space, `RUN.` prefix; the four #372 review cases; token-like ids; decoded-ID order of runs that start at the same timestamp (listed by decoded id, which differs from the JSON-key order in that case); facts of 486 events (9 keys x 27 values, including NaN, +-Infinity, NUL) against 0.1.10's expressions; NaN/Infinity/NUL in run and unrelated tool/attach fields plus the five third-review cases; a run id whose key is over 20 kB, with a NUL; the same rows per reader at 181 and 1,201 events of history; canonical replay/conflict, terminal outcome and capture result transactions, rollback of a failed facts write, session deletion; backfill of 2,345 migration-12 events (batches 1000/1000/345/0); backfill failure at event 1400 of 1,500 (MigrationError naming it, also from the normal open; ledger stays at 12; no `event_run_facts`; events unchanged; after a deliberate repair a rerun applies 13 with 1,500 facts).
- **Differential, 0.1.10 vs 0.1.11** (`probe_352_diff.py`): 3 seeds x 40 sessions of random payloads (4,102 events), each session below the 2,000-event window. Run listings, the run events of 19 run ids and 12 thread projections per session are identical between the two published wheels on each backend: 3,840 compared values on Windows SQLite and on Linux SQLite, and 3,480 on PostgreSQL, where the NUL thread requests are left out. A listing with a scan window smaller than the session is not compared there: 0.1.10 reduced only the oldest window, which is the defect; those cases are covered by the oracle checks above.
- **Request contract.** As the PR states, a conversation/thread *request* id that holds a NUL is outside the contract on PostgreSQL: psycopg takes no NUL in a text parameter, so the existing message read rejects it. Both wheels reject it alike (`psycopg.DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes`, 3 of 3 seeds each); stored NUL values and a run id with a NUL are read on both backends.
- **Observation outside the check.** SQLite and PostgreSQL list different event counts for runs that have `RUN.`-prefixed events: the run listing selects run events with `event_type LIKE 'run.%'`, which is case-insensitive on SQLite, so the 14 `RUN.failed` events of the mixed session count there (2,757 run events) and not on PostgreSQL (2,743). Each backend matches the reduction with its own `LIKE`, and 0.1.10 behaves the same way on each backend (the differential above). This predates #352; the PR's tests note it too. So the reader-output digest is the same on Windows and Linux SQLite (`366f9184...`) and different on PostgreSQL (`77dfe84a...`), and on each backend it is identical across the fresh and upgraded databases.
- **Sizes.** The `probe_352_diff_*.json` files hold every reader result of 120 sessions and are about 820 KB each.
- **Console encoding.** The Windows contract run used `python -I -X utf8`, so its console log keeps non-ASCII run ids; `-X utf8` only sets the console encoding.

### #353 (`probes/probe_353.py`, `probes/probe_353_issue.py`, `probes/probe_353_upgrade.py`)

- **Fresh 0.1.11 store** (`probe_353.py`; SQLite on both machines, PostgreSQL in its own schema per case):
  - The issue's deterministic probe (patched tick, ids `ffff…` then `0000…`) reads `user`, `assistant`, and the newest message is `assistant`.
  - Four tied appends with ids in reverse append order, across two conversations/threads:
    - list order, append-time rowids `[1..4]`, `limit=2` and the newest-message limits 1 and 3;
    - `latest_conversation_id`, the conversation and conversation+thread filters;
    - cursor pages `[1,2]`/`[3,4]`, and a cursor taken at the first append that stays valid after later tied appends;
    - unchanged ids and `created_at`.
  - Also: per-session `append_seq` across a restart, rollback of a failed append, and 4 concurrent handles x 10 tied appends giving `append_seq` 1..40.
  - Compaction through the installed `SessionContextService` (`keep_recent_messages=2`, archive off):
    - The anchor sits at t4, and three more tied appends are all visible after it in append order, in `build_history` too.
    - The second compaction anchors at t7. A restart keeps it.
  - On SQLite, `openminion export transcript` (the installed console script) exports tied messages in append order in JSONL and Markdown, for both id orders.
- **Issue reproduction** (`probe_353_issue.py`, the same script on 0.1.10 as a control):
  - The patched probe reverses on 0.1.10 and is fixed on 0.1.11.
  - With nothing patched, 300 sessions of 4 back-to-back appends on native Windows:
    - 0.1.10: 66 sessions tied and 42 out of order in `list_messages`/`build_history` (11 with the wrong newest message);
    - 0.1.11: 71 tied and 0 out of order.
  - On Linux the clock produced no ties (0/300), so the real-clock form is reported as not applicable there.
- **Upgrade from 0.1.10** (`probe_353_upgrade.py`):
  - 0.1.10 wrote the history at runtime head 11, through `SessionStore.append_message` and `SessionContextService.compact_session`. Its compaction anchored at g3 inside a tied group of six. On 0.1.10, the uncompacted g5 and g6 were already hidden and the compacted g3 and g4 showed again.
  - The 0.1.10 writer exited, and a pre-upgrade backup was taken: SQLite online backup, or `pg_dump -n <schema>`. Then 0.1.11 opened the database: migrations 12 and 13 applied, with ids, bodies, roles and `created_at` unchanged.
  - `append_seq` follows the rowid (append) order on SQLite, and the pre-upgrade `(created_at, id)` order on PostgreSQL.
  - The boundary moves back to e0 (rowid 1, count 1, summary kept). No never-compacted message is hidden, and g3/g4 replay once. The control session keeps its boundary.
  - Restart: nothing re-applied. Post-upgrade tied appends are visible, a post-upgrade compaction keeps the last two, and a second restart keeps that.
  - 0.1.10 opens the SQLite backup at head 11 without migrating.
- Linux run [40097498650](https://github.com/AliceMessi/linux-runner/actions/runs/40097498650). The PostgreSQL seed and upgrade each run in their own process, sharing one schema.

### #361 (`probes/probe_361.py`, `probes/probe_361_cleanup.py`)

- **SDK path,** the issue's reproduction rebuilt without `tests.api.test_artifact_runtime_roots`: a default echo runtime on explicit roots, with every `SQLiteMemoryAuditSink` counted. One turn completes and creates exactly one sink, from the runtime memory assembly, at `.openminion/memory/memory.audit.db`. The turn opens its connection and writes 2 audit events. After `runtime.close()` three times the close counts are `[1]`, `[1]`, `[1]`, the sink holds no connection, and the connection object the turn opened raises `ProgrammingError` on use.
- **Builder:** `build_memory_v2_runtime_assembly` closes the sink it creates once across two `close()` calls, and both calls return the same closed result.
- **`scheduler_still_running`:** with a scheduler stub that stays alive after `stop()`, two `close()` calls return `scheduler_still_running`. The sink and the memory service have 0 closes, and the sink still accepts audit events (2 events). After the scheduler stops, `close()` succeeds, repeats the same result, and closes the sink and the service once each.
- **Borrowed sinks:** `AuditedMemoryStore` still defaults to `owns_sink=False`. A borrowed real `SQLiteMemoryAuditSink` keeps 0 close calls and the same live connection across two store closes, its events stay readable, and its owner can still close it. The owned control (`owns_sink=True`) closes once.
- **Native Windows cleanup** (`probe_361_cleanup.py`): automatic GC is disabled before openminion is imported, `gc.collect()` is wrapped and never called, and nothing is retried. In 5 of 5 fresh runtime roots, right after `runtime.close()`, `memory.audit.db` is renamed, the renamed file is deleted, and one `shutil.rmtree` removes the whole temporary runtime root with no errors. In a separate attributed run, 44 `sqlite3` connections are opened during the turn and none is still live for any file under the runtime root after `runtime.close()`. The same probes on Linux (run 40096925624) pass the same checks; there the attributed turn opens 32 connections and none is live after close.
- The scheduler and builder parts call the installed `openminion.services.runtime.memory` builder directly, because `APIRuntime` cannot inject a scheduler. No remaining handle was found, so nothing is attributed to the migration runner (#373) or to the wrapped store (#368) here.

### #365 (`probes/probe_365.py`)

Every export runs the installed console script (`openminion --home-root <root> --data-root <root>/.openminion --config <root>/config.json export transcript --include-events`) in a subprocess against a disposable runtime database. The store's own `list_messages`/`list_events` order is recorded next to it.
- **Twelve tied events:** the store returns ids 1..12. The JSONL export lists `#1..#12`, and the Markdown payloads come in append order 0..11. The same script on 0.1.10 exports `#1, #10, #11, #12, #2..#9`.
- **Mixed exact ties:**
  - An earlier message, then an event written 2 s ahead, then one turn in one clock tick: 7 setup events, `run.running`, `hello`, `run.responding`, `reply`, `response.persisted` and `run.completed`. The tied message ids sort in reverse. The export is `earlier, hello, reply, #2..#12, #1`, with the Markdown headings and bodies in the same order and `created_at` non-decreasing.
  - Four tied messages interleaved with events, plus ten more tied events: `m0..m3`, then `#1..#14`, for both message id orders.
- The mixed rule is presentation order only. The export command opens the SQLite store at `storage.path`, so these checks ran on SQLite.

### #368 (`probes/probe_368_pg.py`, `probes/probe_368.py`)

PostgreSQL ran on the Linux runner (PostgreSQL 16.15 in `postgres:16`, SQLAlchemy 2.1.6, psycopg 3.3.6), each case in its own schema with its own `application_name`. SQLite, mock and remote ran on both machines with identical results.
- **PostgreSQL SDK turn:** an echo runtime whose memory config (registered on the `ConfigManager`, as upstream's integration test does) selects the PostgreSQL store. One turn completes with one `PostgresMemoryStore` from `resolve_memory_backend`, 9 memory tables in the schema and 1 server connection for the runtime's `application_name` in `pg_stat_activity`. After `runtime.close()` three times the engine disposal count (SQLAlchemy `engine_disposed`) is `[1, 1, 1]`, and 0 connections remain. The disposal comes from `close_runtime_components` -> `RuntimeMemoryAssembly.close` -> `MemoryService.close` -> `AuditedMemoryStore.close` -> `PostgresMemoryStore.close`.
- **Borrowed audited PostgreSQL store:** `PostgresMemoryStore(owns_engine=True)` wrapped in `AuditedMemoryStore(store)` without `owns_store`. Two wrapper closes give 0 disposals and the server connection stays (1); the store still reads its record and accepts a new put. Two closes by the owner then give 1 disposal and 0 connections.
- **PostgreSQL scheduler boundary** (installed builder): while the scheduler stub stays alive, two `close()` calls return `scheduler_still_running` with 0 disposals, 0 artifact-store closes, the server connection still open and the store still usable. After it stops: 1 disposal, 1 artifact-store close, 0 connections, and the repeated close returns the same result.
- **SQLite SDK turn:** one `SQLiteMemoryStore`. The assembly's real `ArtifactCtl` is injected into the store, and the store does not own it. After `runtime.close()` three times the store is closed once and the assembly's `ArtifactCtl` once, and that `ArtifactCtl` rejects a write afterwards (`ProgrammingError: Cannot operate on a closed database.`).
- **sqlite, mock, remote through the builder:** each backend's created store (`SQLiteMemoryStore`, `InMemoryMemoryStore`, `RemoteMemoryStore`) is wrapped in `AuditedMemoryStore`. Two `close()` calls return one closed result, and the close events are exactly `store.close`, `artifactctl.close`. The delegated backend (`BuiltinKnowledgeBackend`) reads through the same `AuditedMemoryStore` object, has no `close()`, and a delegated read finds the written record (sqlite, mock; the remote endpoint is not served, so no read was attempted there).
- **Owned versus injected artifact resources:** with no injected artifact store, the SQLite store creates and owns one real `ArtifactCtl` (explicit roots) when it records an evidence reference, and two assembly closes close it once. A store built with an injected artifact store closes twice without closing it, and `AuditedMemoryStore(store)` without `owns_store` never closes the borrowed store, which stays usable.
- **Scheduler boundary (sqlite, mock):** while the scheduler is alive, no store or artifact close happens and the store stays usable; after it stops, `store.close` and `artifactctl.close` happen once each.
- The builder, injected and scheduler cases call the installed `openminion.services.runtime.memory` builder and `openminion.modules.memory.storage.factory` directly, because `APIRuntime` cannot inject an artifact factory or a scheduler. The gaps listed under "Left out" in #380 (external provider close, fallback retention, construction-failure cleanup, typed remote config, PostgreSQL `_connect`) are separate and were not checked.

### #373 (`probes/probe_373.py`)

The probe calls `gc.disable()` before importing openminion and wraps `gc.collect()` to count calls; the count is 0 at the end. Connections are tracked by wrapping `sqlite3.connect` with strong references, so an unclosed connection cannot be freed before it is checked. Nothing is retried.
- **Fresh migration:** `migrate(target="head")` runs on a new, empty file for 14 modules. Each call opens 9 connections: `detect()` x3, the backup snapshot x2, the Alembic upgrade x2, metadata x1 and `run_verification()` x2. All 9 are closed when it returns, and the public metadata step has committed `schema_head` and `last_migrated_at`. A second, untracked fresh file per module is renamed and deleted right after `migrate()` returns.
- **Detection/verification:** `detect()` and `verify()` each open one connection and close it, on a module DB and on a migrated DB (`level="full"`). In a real echo runtime build, the migration runner and verification sites open 18 connections, to `store.sqlite`, `policy.db`, `sessions.db` and two backup temp files. All 18 are closed after `runtime.close()`.
- **Metadata and hooks:**
  - A successful metadata write is committed and its connection closed.
  - A failing `_upsert_sqlite_om_meta`:
    - Called directly, it propagates its error, its row is rolled back and the connection is closed.
    - Through `migrate()`, the report has `success=False`, `error="forced metadata failure"` and `rolled_back=True`. The snapshot is restored, all connections are closed and the file can be renamed right away.
  - A failing `verifier_hook`:
    - Through `runner.verify()`, its error propagates, its write is rolled back and the owned connection is closed.
    - Through `migrate()`, the pre-migration verification raises out of `migrate()` and nothing is migrated.
    - With a borrowed connection (`run_verification(connection=...)`), the error propagates, the connection stays open, and the hook's row stays pending in the caller's transaction until the caller rolls back.
- **Native Windows SDK turn:** 5 fresh roots, one after another. One echo turn `completed`, then `runtime.close()`. Right after that, `os.replace()` and `os.remove()` of `authored_tools/store.sqlite` succeed, and `shutil.rmtree()` removes the whole runtime root.
- **Control:** the same probe on the published 0.1.10 fails 13/26.
  - 6 of the 9 connections per `migrate()` stay open.
  - The detect, verify and metadata connections stay open.
  - The SDK case fails 5/5 with `PermissionError [WinError 32]`.
  - The `migrate()` auto-rollback fails with `PermissionError [WinError 5]` while restoring the snapshot.
- **Linux:** 26/26. Only the closure checks mean something there, because an open handle does not block unlink on Linux.

### #378 (`probes/probe_378.py`)

GC is set up as for #373: disabled before import, `gc.collect()` never called (0 calls counted), no retries, strong-reference connection tracking.
- **Rows preserved:**
  - A `secret` DB at head with 5 rows is exported and imported into a new file. `alembic_version`, `om_meta` and `secrets` match the source row for row.
  - The runtime-written `telemetry.db` from one echo SDK turn (202 `events` rows plus `om_meta`) round-trips the same way.
- **Connections:** on success the import opens `_import_omx_tables_sqlite` (`transfer.py:519`) and `run_verification` (`verify.py:112`). Both are closed on return.
- **Forced mid-import failures,** each into a target that already holds a different secret DB:
  - The PR test's failure: `_replace_sqlite_table_rows` raises after writing `secrets`, the last table, after `alembic_version` and `om_meta` were replaced in the same transaction.
  - An unpatched failure: the bundle's `secrets.jsonl` is altered after export, so the checksum check fails while `secrets` is being imported.

  Both report `success=False` and keep the raised error. Every table still holds the previous rows, and the import's one connection is closed.
- **Native Windows cleanup:** without tracking, the target is renamed and deleted right after `import_omx()` returns, and its directory is removed. This works after the successful import and after both failed imports.
- **Borrowed record stores:** `import_omx(target_record_store=...)` and `export_omx(record_store=...)` never call `close()` on the `RecordStoreSQLite` the probe owns. Both stay usable, the target holds the 5 imported values, and the owner's own `close()` is the first and only close.
- **Control:** the same probe on 0.1.10 fails 6/19, all of them closure checks. The `transfer.py:518` connection stays open after success and after both failures, and renaming the target fails with `PermissionError [WinError 32]`.
- **Linux:** 19/19. These are closure checks only.

## Controls on the published 0.1.10

Several probes were also run against `openminion==0.1.10` from PyPI, to show that they detect the defect the release fixes.
- **#373, #378:** the same probes, unchanged, fail on 0.1.10.
  - `probe_373`: 13 of 26 checks fail. Migration, detection, verification and metadata connections stay open, the SDK-turn rename and rmtree fail with `PermissionError [WinError 32]`, and the `migrate()` auto-rollback fails with `[WinError 5]`.
  - `probe_378`: 6 of 19 checks fail. The import connection stays open, and the target rename fails with `[WinError 32]`.
- **#353, #365:** the control runs assert that the old defect reproduces on 0.1.10.
  - `probe_353_issue`: the tied pair reverses, and 42 of 66 real-clock tied sessions on Windows read out of order.
  - `probe_365`: tied events export as `1,10,11,12,2..9`.
- **#352:** 0.1.10 is the writer for the upgrade cases, and the differential baseline for sessions below the 2,000-event window.

The control outputs are [`windows/control-0.1.10/`](windows/control-0.1.10), [`windows/probe_353_issue-v0110.json`](windows/probe_353_issue-v0110.json), [`linux/353/probe_353_issue-v0110.json`](linux/353/probe_353_issue-v0110.json), [`windows/probe_365-v0110-control.json`](windows/probe_365-v0110-control.json) and [`linux/365/probe_365-v0110-control.json`](linux/365/probe_365-v0110-control.json).

## Observations outside the acceptance checks

- **In-memory connections from context compression** (from `probe_373` part G). After `runtime.close()`, two in-memory SQLite connections are still open. One comes from `modules/context/compress/storage/store.py` via `module_store.py`; the other is the default `SQLiteCheckpointStore(":memory:")` created at `compaction.py:214`.
  - The migration owners did not open them, and they have no file, so they don't affect any rename/delete check here.
  - They are listed under `observed.G_observation_other_open_connections` in [`windows/probe_373.json`](windows/probe_373.json).
- **`LIKE 'run.%'` differs between backends** (from #352). The run listing selects run events with `LIKE 'run.%'`, which is case-insensitive on SQLite and case-sensitive on PostgreSQL. So `RUN.`-prefixed events count as run events on SQLite only: 2,757 against 2,743 in the mixed session. Each backend matches the reduction with its own `LIKE`, and 0.1.10 behaves the same way, so this predates #352.
- **The transcript export reads the SQLite store** (from #353/#365). `openminion export transcript` opens the SQLite store at `storage.path`, so the transcript checks ran on SQLite only. I did not check what it does under `storage.backend: postgres`.
- **Path tokens on Linux.** On the runner, `<WORK>` and `<RUNNER_TEMP>` both stand for `$RUNNER_TEMP`. The redactor picks either token, because the two paths are the same.

## Files

- `provenance.json`: the PyPI metadata, wheel hash comparison and RECORD check for 0.1.11 on Windows and Linux, and for the 0.1.10 venv. It also has the GitHub tag/commit and the containment of the seven merges.
- `probes/`: the scripts as run.
  - `_common.py`: banner, checks, redaction, the echo runtime without repo fixtures, and `sqlite3.connect` tracking.
  - `provenance.py`.
  - The `probe_*.py` probes.
  - `linux_setup.sh` and `steps_*.sh`: the runner setup and steps. They install from PyPI into new venvs and run each probe with `python -I` from an empty directory. The runner checkout is the runner repo itself, so no openminion source is present.
- `windows/`: native Windows outputs (`.json` with every check and its observed values, `.log` with the console output), plus `windows/control-0.1.10/` and `windows/probe-sha256.txt`.
- `linux/<issue>/`: the runner outputs per issue. Each folder also holds `run-id.txt` (the Actions run), `os.txt`, `postgres-version.txt`, `pip-show*.txt`, `provenance-linux.json` and `probe-sha256.txt` (the sha256 of each shipped probe, equal to the files in `probes/`).
- `linux/provenance/`: the provenance-only runner run.

Runner runs on [AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner/actions):

| Run | Content |
|---|---|
| [40096190597](https://github.com/AliceMessi/linux-runner/actions/runs/40096190597) | provenance only |
| [40097780930](https://github.com/AliceMessi/linux-runner/actions/runs/40097780930) | #352 |
| [40097498650](https://github.com/AliceMessi/linux-runner/actions/runs/40097498650) | #353, #365 |
| [40096925624](https://github.com/AliceMessi/linux-runner/actions/runs/40096925624) | #361, #368 |
| [40097100902](https://github.com/AliceMessi/linux-runner/actions/runs/40097100902) | #373, #378 |

Earlier runs 40097408900 (#352) and 40097246335 (#353/#365) were superseded and are not used. The first lacked the decoded-ID ordering case, which was added afterwards. The second had a real-clock tie precondition on Linux that was later made informational.

The outputs contain no keys, credentials or personal data. The runner's fixture PostgreSQL URL is never printed. Local paths are replaced with `<VENV>`, `<VENV0110>`, `<WORK>`, `<TMP>`, `<RUNNER_TEMP>` and `<USER_HOME>`. Every file was then scanned for local paths and user names: no hits.
