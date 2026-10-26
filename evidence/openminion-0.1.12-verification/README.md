# Release verification of openminion 0.1.12 (installed package)

This folder covers the published-package checks the maintainer asked for on openminion/openminion #364, #369, #388, #389, #391, #392, #395, #396, #397, #400, #402, #403 and #408. These issues are labelled `status:awaiting-release`. Their fixes (PRs #385, #406, #398, #404, #394, #399, #422, #417, #421, #415, #416, #418 and #414) are in 0.1.12, which was published on PyPI and GitHub on 26 Oct 2026.

Everything below ran against `openminion==0.1.12` installed from PyPI into fresh virtual environments, not against a source checkout. The one exception is #408, a test-only fix, whose twelve tests ran from the `v0.1.12` tag source, as its checklist asks. The probes are standalone scripts that use only the installed package; the wheel does not ship the repo's `tests/` fixtures. The SQLite and native Windows cases ran on native Windows 11. The PostgreSQL cases, plus Linux parity runs, ran on a GitHub-hosted Ubuntu runner that installed the same wheel from PyPI and had no openminion checkout. The upgrade cases created their starting databases with the published `openminion==0.1.11`, and the negative controls ran the same probes (or the original reproductions) on 0.1.11. Each checklist is the maintainer's latest comment on its issue, posted after the fix merged. Everything ran on 26 Oct 2026.

## Package provenance

| | |
|---|---|
| Install (Windows) | `uv venv --python 3.12.11 venv` and `uv pip install --python venv/Scripts/python.exe --index-url https://pypi.org/simple --refresh "openminion==0.1.12"` into a new venv; `openminion==0.1.11` the same way in a second venv for the controls |
| Install (Linux) | [`probes/linux_setup.sh`](probes/linux_setup.sh): `uv venv --python 3.12` and `uv pip install --index-url https://pypi.org/simple "openminion[postgres]==0.1.12"` (plus `openminion[postgres]==0.1.11` in a second venv for the seeds and controls) |
| Wheel | `openminion-0.1.12-py3-none-any.whl`, uploaded to PyPI 2026-10-26T09:37:35Z |
| sha256 (PyPI JSON) | `0d9c241e8575a42d54169d50a3db112c23f9980737a593d914ce30090ca74812` |
| sha256 (downloaded wheel) | `0d9c241e8575a42d54169d50a3db112c23f9980737a593d914ce30090ca74812` (match, on both machines) |
| Installed files | All 2744 hashed RECORD entries of the wheel are in the installed RECORD and match the installed files' sha256: 0 mismatched, 0 missing (Windows and Linux) |
| `pip show` (`uv pip show`) | `Name: openminion`, `Version: 0.1.12`, `Location: <VENV>\Lib\site-packages` (Windows), `<VENV>/lib/python3.12/site-packages` (Linux) |
| Imported module | Every probe prints `openminion.__file__` (under `<VENV>`'s `site-packages`) and `__version__` = `0.1.12`. Probes run from empty directories outside any checkout, with `python -I` |
| Windows | Native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4 |
| Linux | Ubuntu 24.04.5 LTS GitHub-hosted runner ([AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner), preset `none`: the checkout is the runner repo itself, not openminion), CPython 3.12.15, SQLite 3.45.1, PostgreSQL 16.15 (`postgres:16`), SQLAlchemy 2.1.6, psycopg 3.3.6 |
| 0.1.11 (seeds and controls) | `openminion==0.1.11` from PyPI in a separate venv: wheel sha256 `90bbe2bc1fe0f57bc74dbe478c96411006ccc2429295e12fe0b05fd79db6e29b` matches PyPI, and all 2743 RECORD entries match (Windows and Linux) |

Details are in [`provenance.json`](provenance.json), [`probes/provenance.py`](probes/provenance.py), [`windows/provenance/`](windows/provenance) and [`linux/provenance/`](linux/provenance) (provenance-only runner run [40303431294](https://github.com/AliceMessi/linux-runner/actions/runs/40303431294)). Every runner run repeats the check in its own `provenance-linux.json`, and its `probe-sha256.txt` shows that the probes it ran are byte-identical to the files in `probes/`.

## Release source contains each fix

Checked first, before any probe, with [`probes/release_source.py`](probes/release_source.py) (output [`release-source/release_source.json`](release-source/release_source.json)) and the GitHub API ([`release-source/github-api.txt`](release-source/github-api.txt)):

- The GitHub release `v0.1.12` was published 2026-10-26T09:39:51Z. The annotated tag `e0bf1e35` points to commit `18f9d875` ("Merge pull request #426 from openminion/dev").
- For each of the 13 fixes, both the PR's merge commit and its reviewed head commit are ancestors of `18f9d875`: `git merge-base --is-ancestor` in a clone with the tag fetched, and `behind_by: 0` from the GitHub compare API. That is 26 of 26 commits.
- The published archives equal the tag source. Every one of the 2738 `openminion/` files in the PyPI wheel is byte-identical to `src/openminion/` at the tag. Every one of the 2743 tag-tracked files in the PyPI sdist (`openminion-0.1.12.tar.gz`, sha256 `33fa9208dc544bb8efd3af8463564ea4f10f79c62c0d84be13619e9f90a9711f`) is byte-identical to the tag; the other 8 sdist files are generated metadata (`PKG-INFO`, `setup.cfg`, `egg-info`). So every packaged file each fix touched ships as it is at the tag. The only touched source file that is not packaged is `src/openminion/modules/memory/README.md` (#398, #421), which is documentation.
- Some files that a PR touched changed again after its head commit, through later merges in the same release, mostly the CHANGELOG. They are listed per fix under `files_changed_after_pr_head`, which is why the probes test behaviour on the installed package and not file identity with the PR heads.

| Issue | PR | Merge commit | Reviewed head |
|---|---|---|---|
| #364 | #385 | `c9578ab6` | `36c6e0fe` |
| #369 | #406 | `6685ec8f` | `df20c295` |
| #388 | #398 | `6427a771` | `6f1d29bc` |
| #389 | #404 (via #423) | `fbc49382` | `8fce7760` |
| #391 | #394 | `40ae42e1` | `f40f31eb` |
| #392 | #399 (via #423) | `bc1cafb2` | `82f9d4ce` |
| #395 | #422 (via #423) | `b486882b` | `52265032` |
| #396 | #417 (via #423) | `fc765eae` | `7f8a962f` |
| #397 | #421 | `16288bd7` | `62a8819c` |
| #400 | #415 | `b670bbb6` | `529c76c6` |
| #402 | #416 | `c3a5bb53` | `5e03d180` |
| #403 | #418 (via #423) | `27e5d582` | `6af048ee` |
| #408 | #414 | `a41487ab` | `d69a9dc5` |

## Results

Every check below ran on the published 0.1.12, and nothing failed on it. One sub-item could not be run as written: on #364, the real `openminion room` CLI against a PostgreSQL runtime database, because `APIRuntime` does not start with `storage.backend: postgres` (on 0.1.11 as well). The #364 section says what ran instead. The first-empty-entry control of #395 ran at function level, because the SDK rejects an empty message before any brain step.

| Issue | Maintainer's checklist (published package) | Result | Evidence |
|---|---|---|---|
| #364 | Fresh room with three tied joins and reversed row ids, through the storage API, the room CLI and the routing consumer; active upsert, leave/rejoin, reopen, explicit selection, legacy owner; concurrent joins and overlapping leave/rejoin; coordinated upgrade of a populated migration-13 database (order kept, left participants included, new sequence after, migration-14 index/ledger, rerun idempotence, failure rollback); no older writer, rollback from the backup, historical order and wall clock unchanged. SQLite and PostgreSQL | **PASS** on SQLite (44/44 Windows, 44/44 Linux; upgrade 25/25 each). PostgreSQL 16: 44/44 and upgrade 24/24, **but the room CLI itself was NOT RUN on PostgreSQL** (startup error above); the CLI handlers and routing consumer ran on the PostgreSQL store instead | [`windows/364/`](windows/364), [`linux/364/`](linux/364) |
| #369 | Coordinated upgrade; fresh databases and a pre-migration-15 history on SQLite and PostgreSQL: migration 15 applied once, raw metadata preserved, malformed JSON and extraction failures roll back migration and ledger; installed ACP `session/list` and Focus directory sessions: matching/nonmatching and multiple filters, ordering, limits; SQLite legacy behavior; PostgreSQL literal top-level strings with NUL/surrogates/NaN in unrelated members, long values, collision controls; inserts, replacements, ignored conflicts and deletes keep the rows consistent; concurrent PostgreSQL writers and rollback; falsey-filter normalization | **PASS** (filters, writes, concurrency: 132/132 PostgreSQL 16, 97/97 Linux SQLite, 97/97 Windows SQLite; upgrades from 13 and from 14, malformed and extraction-failure rollbacks: all pass on each backend). On PostgreSQL the installed ACP and Focus handlers ran in process over a PostgreSQL `SessionStore`, as upstream's own PostgreSQL ACP test does, because `APIRuntime` does not start there | [`windows/369/`](windows/369), [`linux/369/`](linux/369) |
| #388 | Real runtime with an owned external backend closes backend and artifact store once over repeated `runtime.close()`; no-close and borrowed backends; external provider with a PostgreSQL `memory.store` opens no engine (server connections); default provider and scheduler boundary | **PASS** (13/13 Windows, GC disabled; 15/15 Linux + PostgreSQL 16) | [`windows/388/`](windows/388), [`linux/388/`](linux/388) |
| #389 | Direct `build_agent_memory_service` callers and the `GatewayService`-built fallback on SQLite and PostgreSQL, closed twice: owned handles close once, engine disposed once; borrowed adapters stay open; external backend with and without `close()`; scheduler boundary | **PASS** (13/13 Windows, GC disabled; 16/16 Linux + PostgreSQL 16) | [`windows/389/`](windows/389), [`linux/389/`](linux/389) |
| #391 | Real memctl YAML with `store.backend: remote`, `memctl list` against a controlled remote store, configured timeout and retries including 0; whole-config and store-section construction; dictionary precedence/defaults; missing-endpoint validation | **PASS** (24/24 Windows, 24/24 Linux) | [`windows/391/`](windows/391), [`linux/391/`](linux/391) |
| #392 | Audited PostgreSQL runtime/SDK session lifecycle changes the intended seeded records (decay, compression, capacity, insights), records with evidence included; not reapplied; no `_connect` warning; SQLite and direct-store controls | **PASS** (38/38 Linux SQLite + PostgreSQL 16; 18/18 Windows SQLite) | [`windows/392/`](windows/392), [`linux/392/`](linux/392) |
| #395 | SDK same-turn continuation without new input or typed contract keeps the freshness contract, obligations and diagnostics; typed replacement, new input and first-empty-entry controls; provider/model recorded | **PASS** (24/24 Windows, 24/24 Linux; scripted OpenAI-compatible provider on 127.0.0.1; first empty entry at function level) | [`windows/395/`](windows/395), [`linux/395/`](linux/395) |
| #396 | Mixed `run.`/`RUN.`/`Run.` events with the same run ids: run summaries and exact-run reads use only the lowercase prefix on both backends; state/count/start/end facts, tied ordering, suffix handling, bounded rows; generic event readers and migration-15 metadata filters unchanged | **PASS** (22/22 Windows SQLite; 42/42 Linux: 22 SQLite, 20 PostgreSQL 16) | [`windows/396/`](windows/396), [`linux/396/`](linux/396) |
| #397 | Two `reference-sqlite` SDK turns complete; captures stay pending with `CAPTURE_RECOVERY_UNSUPPORTED`, no invented receipt, recovery leaves them pending; a capturing backend gets the real bundle, returns its receipt, replays; its failures propagate; default capture and repeated shutdown | **PASS** (14/14 Windows, 14/14 Linux) | [`windows/397/`](windows/397), [`linux/397/`](linux/397) |
| #400 | PostgreSQL JSONB evidence lists kept; decay, summary retention, capacity, stale insights; purge of soft-deleted records and promoted/rejected candidates; only the expected artifact edges removed; SQLite text/empty/None unchanged, invalid JSON raises; history written by 0.1.11 and new evidence | **PASS** (72/72 Linux SQLite + PostgreSQL 16; 36/36 Windows SQLite). The audited purge path on PostgreSQL is #401, not in 0.1.12, not claimed | [`windows/400/`](windows/400), [`linux/400/`](linux/400) |
| #402 | Real `memctl inspect` JSON and human modes report depth 2 through the audited store; empty history gives no summary; non-SQLite stores unchanged (PostgreSQL); stats/trace unchanged; command lifecycle closes resources on success and failure | **PASS** (15/15 Windows; 20/20 Linux + PostgreSQL 16) | [`windows/402/`](windows/402), [`linux/402/`](linux/402) |
| #403 | Native Windows, GC disabled: successful and failing in-process memctl writes close service/store/audit once at command exit, files renamed/deleted at once; borrowed services stay open; direct factory callers own their close; read/write commands, #416 history and #394 remote config still work | **PASS** (26/26 native Windows with GC disabled, `gc.collect` 0 calls; 26/26 Linux) | [`windows/403/`](windows/403), [`linux/403/`](linux/403) |
| #408 | Test-only: the release-tag source contains #414; the twelve tests from the tag pass on native Windows with GC unable to hide handles; tempdir cleanup and restore succeed, assertions intact | **PASS** (tag contains all 52 added lines, no bare `sqlite3.connect` left; 12/12 passed in 5 of 5 runs with GC disabled, 0 open connections after teardown) | [`windows/408/`](windows/408) |

The comment posted on each issue lists every checklist item with what ran and what was observed. The `.json` outputs hold every check with its observed values; the `.log` files hold the console output.

### #364 (`probes/probe_364.py`, `probe_364_lib.py`, `probe_364_upgrade.py`, `steps_364.sh`)

- Tied joins use the issue's technique: the participant clock is patched to one timestamp, and each new row id sorts before the previous one.
- Concurrency: 12 peers x 10 tied joins on PostgreSQL (8 x 10 on Linux SQLite, 4 x 5 on Windows), a rejoin started inside the leave's transaction, and a leave/rejoin storm of 2,400 operations on PostgreSQL with 0 errors. #393 (first-active-agent race) is not certified.
- Upgrade: seeded by 0.1.11 in its own process (runner: 500 sessions, 2,158 participants, 99 left; Windows: 150 sessions, 623 participants). Backups with the 0.1.11 `storage backup --sqlite` and `pg_dump -Fc`. Two injected failures inside migration 14 each roll back to 13 with rows unchanged. Every row's `join_seq` matches its pre-upgrade `(joined_at, id)` place.
- **Not run as written:** the real `openminion room` CLI on PostgreSQL. `APIRuntime.from_config_path` with `storage.backend: postgres` fails with `RuntimeError: runtime record_store does not expose sqlite connection` (`api/core/bootstrap.py:126`) on 0.1.11 and 0.1.12 alike. The installed CLI parser and the `room create`/`room invite` handlers ran with only `_open_room_runtime` returning the PostgreSQL session store, and the routing consumer ran on that store.
- Runner run [40306296935](https://github.com/AliceMessi/linux-runner/actions/runs/40306296935). Run 40305632500 was superseded (it found the PostgreSQL CLI problem) and is not used.

### #369, #396 (`probes/probe_369_filters.py`, `probe_369_seed.py`, `probe_369_upgrade.py`, `probe_369_lib.py`, `probe_396.py`, `steps_369.sh`, `steps_369_windows.sh`, `steps_396_windows.sh`)

- The ACP server needs the `acp` extra, so on Windows these probes ran in two further fresh venvs with `openminion[acp]==0.1.12` and `openminion[acp]==0.1.11` (`agent-client-protocol` 0.12.1). Their provenance is in [`windows/provenance/provenance-windows-acp*.json`](windows/provenance): the same wheels, every RECORD entry matching. The runner installed `openminion[postgres,acp]`.
- Upgrade: each history (1,243 sessions with NaN, Infinity, an escaped NUL, lone surrogates, a 100,000-character value and non-string values) was written by a 0.1.11 process that then exited. A pre-upgrade backup was taken (the 0.1.11 `storage backup --sqlite`, or `pg_dump`), then 0.1.12 upgraded it, from 13 and from 14. 0.1.11 only ever opened the restored backups (8 of 8 intact). Malformed JSON and an injected extraction failure each leave the ledger at 14 with no table, and a rerun after the repair applies 15 once. On SQLite the injected extraction error surfaces as the raw `RuntimeError` rather than `MigrationError` (PostgreSQL wraps it); the rollback is complete either way.
- SQLite: the real `openminion acp` server over stdio JSON-RPC and Focus `OpenMinionRuntime` over a real `APIRuntime`. PostgreSQL: the installed `OpenMinionACPAgent` and Focus runtime in process over a PostgreSQL runtime `SessionStore`, as upstream's `test_postgres_acp_session_list_filters_by_surface` does, because `APIRuntime` does not start on PostgreSQL runtime storage (see the observations).
- #396 also reads through the runs API and, on SQLite, the installed `openminion status runs` / `status run-events` CLI. Summaries are compared with an oracle computed in Python from all of the session's events.
- The `W=` and `E=` lines of `steps_369_windows.sh` and `steps_396_windows.sh` held absolute local paths when they ran. Before publishing they were replaced by required variables, the only change, and the two hash lines in `windows/369/probe-sha256.txt` and `windows/396/probe-sha256.txt` were updated to match.
- Runner run [40308568044](https://github.com/AliceMessi/linux-runner/actions/runs/40308568044). Run 40307070890 was superseded and is not used.

### #388, #389, #397 (`probes/probe_388.py`, `probe_389.py`, `probe_397.py`, `probe_388_lib.py`, `steps_388_389_397.sh`)

- #388 and #389 call `gc.disable()` before importing openminion and never call `gc.collect()` (wrapped to count; 0). #397 runs with GC on and makes no GC claim.
- PostgreSQL connection counts come from `pg_stat_activity`, filtered by each case's own `application_name`.
- Runner run [40305242150](https://github.com/AliceMessi/linux-runner/actions/runs/40305242150).

### #392, #395, #400 (`probes/probe_392.py`, `probe_395.py`, `probe_400.py`, `steps_392_395_400.sh`)

- #392 seeds the store the SDK runtime creates and reads `memory_records` server-side after each turn; the artifact edges come from a real artifact in the runtime's own artifact store.
- #395 drives the SDK's `openai` provider against a scripted OpenAI-compatible server on 127.0.0.1 (no external network; default model id `gpt-4.1-mini` in the requests).
- #400's history case writes the items with the published 0.1.11 in its own venv and process, into new PostgreSQL schemas and SQLite files, before 0.1.12 reads them.
- Runner run [40305483503](https://github.com/AliceMessi/linux-runner/actions/runs/40305483503). Run 40305151141 was superseded: a probe bug in #392 counted two `decision` records that the turns write themselves.

### #391, #402, #403 (`probes/probe_391.py`, `probe_402.py`, `probe_403.py`, `run_391_402_403_windows.sh`, `steps_391_402_403.sh`)

- #391's remote store is a local HTTP server on 127.0.0.1 that accepts the `memory_transport.v1` envelope and counts requests.
- #402 and #403 run memctl in process through typer's `CliRunner` with GC disabled, and the real console script for the inspect modes.
- Runner run [40306932228](https://github.com/AliceMessi/linux-runner/actions/runs/40306932228).

### #408 (`probes/check_408_source.py`, `gcguard_408.py`, `run_408_windows.sh`)

A source-test check of the tag, separate from the installed-package checks. The twelve tests ran from a clean detached checkout of `v0.1.12` with `PYTHONPATH="src;."`, in a venv holding only the tag's dependencies and dev extra. The `gcguard_408` pytest plugin disables the collector before the tests import, turns `gc.collect` into a counting no-op inside tests, lists file-backed connections still open after teardown and renames every `tmp_path` file. Source check: [`windows/408/check_408_source.json`](windows/408/check_408_source.json).

## Controls on the published 0.1.11

Every probe also ran against `openminion==0.1.11` from PyPI in a separate venv (or, for #408, the same twelve tests from the `v0.1.11` tag source), to show that it detects the defect the release fixes:

| Issue | 0.1.11 result | What fails |
|---|---|---|
| #364 | 23 of 44 fail (Windows SQLite, Linux SQLite, PostgreSQL) | tied joins list `third, second, first`; removing `first` selects `third`; broadcast routes `third, fourth, second, first` |
| #369 | 81 of 85 fail (PostgreSQL); SQLite 64/64 pass (the bug is PostgreSQL-only) | `function json_extract(text, unknown) does not exist`, including the in-process ACP `session/list` and Focus listing |
| #388 | 10 of 13 fail (Windows), 11 of 15 (Linux + PostgreSQL) | external-provider close raises `AttributeError`; artifact store left open (`WinError 32`); a PostgreSQL `memory.store` opens a connection that stays open |
| #389 | 11 of 13 fail (Windows), 12 of 16 (Linux + PostgreSQL) | the fallback adapter has no `close()`; a gateway that built it closes nothing (`WinError 32`, PostgreSQL connection left open) |
| #391 | 12 of 24 fail (Windows and Linux) | typed remote config raises `remote backend requires remote.endpoint`; `memctl list` exits 1 |
| #392 | 12 of 38 fail (PostgreSQL); the Windows SQLite-only run passes 18/18 | `'PostgresMemoryStore' object has no attribute '_connect'` on every turn; no record changes |
| #395 | 8 of 24 fail (Windows and Linux) | the continuation replaces the contract with the empty-input default; the judge's unverified answer closes the turn |
| #396 | 16 of 21 fail (Windows SQLite), 17 of 40 (Linux: 16 SQLite, plus the metadata filter on PostgreSQL, which is #369); the 18 PostgreSQL run-reader checks pass (the bug is SQLite-only) | `RUN.`/`Run.` events counted in run summaries, events and state; the runs API and `openminion status` list a `RUN.`-only run |
| #397 | 5 of 14 fail (Windows and Linux) | `NotImplementedError` out of `Agent.run` |
| #400 | 20 of 72 fail (Linux + PostgreSQL), 2 of 36 (Windows) | `JSONDecodeError` in the decoder and in every PostgreSQL retention and purge step |
| #402 | 5 of 15 fail (Windows), 5 of 20 (Linux) | no `history_summary` through the audited store; command resources never closed |
| #403 | 17 of 26 fail (Windows and Linux) | close counts 0/0/0; renaming the audit database fails with `WinError 32` |
| #408 | 12 of 12 tests fail in 2 of 2 runs with GC disabled; 11 of 12 with default GC | `WinError 32` on tempdir cleanup, `WinError 5` on restore |

The control outputs are in each issue's `windows/<issue>/control-0.1.11/` and in `linux/<issue>/` (files with `_0111` in the name).

## Observations outside the acceptance checks

- **`APIRuntime` does not start on a PostgreSQL runtime database** (from #364). With `storage.backend: postgres`, `APIRuntime.from_config_path` fails with `RuntimeError: runtime record_store does not expose sqlite connection` at `api/core/bootstrap.py:126`, on 0.1.11 and 0.1.12. This is why the room CLI could not run on PostgreSQL.
- **The bootstrap fallback's artifact index ignores the given `data_root`** (from #389). `build_agent_memory_service` creates the fallback's artifact index under `OPENMINION_DATA_ROOT`, not the `data_root` passed to it. The same on 0.1.11; it does not affect the closes.
- **`browser-playwright` folders in the working directory** (from #364 and #388). Building an echo `APIRuntime` with explicit `home_root` and `data_root` still creates empty `.openminion/browser-playwright/{artifacts,downloads}` folders under the process's working directory, on 0.1.11 and 0.1.12. These may be intended workspace defaults.
- **A dictionary remote config with `max_retries: 0` gets 1 retry** (from #391), through the existing `or` default. Unchanged from 0.1.11; the typed config keeps 0.
- **Windows error text is localized.** The Windows machine reports `WinError` messages in its OS language (Italian), so some control logs show them that way.
- **A lone-surrogate filter value on SQLite raises `UnicodeEncodeError`** (from #369), from the `sqlite3` driver, on 0.1.11 and 0.1.12 alike. It is not in the documented list of backend differences.
- **The upgrade guidance is not in the published archives.** `docs/runtime-database-upgrades.md` and the CHANGELOG are not in the wheel or the sdist, so the upgrades followed the document at the tag and the release notes.

## Files

- `provenance.json`: the PyPI metadata, wheel hash comparison and RECORD check for 0.1.12 and 0.1.11 on Windows and Linux, plus the release-source summary.
- `release-source/`: `release_source.json` and `.log` (ancestry of each fix's commits, archives against the tag), `prs.json` (each PR's merge commit, head and files from the GitHub API, the script's input), `github-api.txt` (release, tag and compare results).
- `probes/`: the scripts as run.
  - `_common.py`: banner, checks, redaction, the echo runtime without repo fixtures, and `sqlite3.connect` tracking.
  - `provenance.py`, `release_source.py`.
  - The `probe_*.py` probes and their `*_lib.py` helpers; `check_408_source.py` and `gcguard_408.py` for #408.
  - `linux_setup.sh` and `steps_*.sh`: the runner setup and steps. They install from PyPI into new venvs and run each probe with `python -I` from an empty directory. The runner checkout is the runner repo itself, so no openminion source is present. `run_*_windows.sh` and `steps_*_windows.sh` are the Windows sequences.
- `windows/<issue>/`: native Windows outputs, `probe-sha256.txt` (the sha256 of each probe file as run) and `control-0.1.11/`. `windows/provenance/`: the Windows provenance runs.
- `linux/<issue>/`: the runner outputs per issue. Each folder also holds `run-id.txt` (the Actions run), `os.txt`, `postgres-version.txt`, `pip-show*.txt`, `provenance-linux.json` and `probe-sha256.txt` (the sha256 of each shipped probe, equal to the files in `probes/`).
- `linux/provenance/`: the provenance-only runner run.

Runner runs on [AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner/actions):

| Run | Content |
|---|---|
| [40303431294](https://github.com/AliceMessi/linux-runner/actions/runs/40303431294) | provenance only (0.1.12 and 0.1.11) |
| [40306296935](https://github.com/AliceMessi/linux-runner/actions/runs/40306296935) | #364 |
| [40308568044](https://github.com/AliceMessi/linux-runner/actions/runs/40308568044) | #369, #396 |
| [40305242150](https://github.com/AliceMessi/linux-runner/actions/runs/40305242150) | #388, #389, #397 |
| [40305483503](https://github.com/AliceMessi/linux-runner/actions/runs/40305483503) | #392, #395, #400 |
| [40306932228](https://github.com/AliceMessi/linux-runner/actions/runs/40306932228) | #391, #402, #403 |

The outputs contain no keys, credentials or personal data. The runner's fixture PostgreSQL URL is never printed. Local paths are replaced with `<VENV>`, `<VENV0111>`, `<WORK>`, `<TMP>`, `<RUNNER_TEMP>` and `<USER_HOME>`. Every file was then scanned for local paths and user names: no hits.
