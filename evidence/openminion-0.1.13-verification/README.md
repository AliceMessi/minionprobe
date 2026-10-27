# Release verification of openminion 0.1.13 (installed package)

This folder covers the published-package checks the maintainer asked for on openminion/openminion #412, #413, #420, #401 and #419. These issues are labelled `status:awaiting-release`; each checklist is the maintainer's comment after the fix was merged. The fixes (#428, #431, #429, #433 and #430) were merged after 0.1.12 and are released in 0.1.13.

Everything below ran against `openminion==0.1.13` installed from PyPI into fresh virtual environments, not against a source checkout. The controls ran the same probes against `openminion==0.1.12` from PyPI in separate environments.

## Package provenance

| | |
|---|---|
| Install (Windows) | `uv venv --python 3.12.11 venv` and `uv pip install --python venv/Scripts/python.exe --index-url https://pypi.org/simple --refresh "openminion==0.1.13"` into a new venv; `openminion==0.1.12` the same way in a second venv for the controls |
| Install (Linux) | [`probes/linux_setup.sh`](probes/linux_setup.sh): `uv venv --python 3.12` and `uv pip install --index-url https://pypi.org/simple "openminion[postgres]==0.1.13"` (plus `openminion[postgres]==0.1.12` in a second venv for the controls) |
| Wheel | `openminion-0.1.13-py3-none-any.whl`, uploaded to PyPI 2026-10-27T04:20:50Z |
| sha256 (PyPI JSON) | `266033a0a438b2fccc64b5ffec7a7929789108a4962347d6d57dfeb1cfd55897` |
| sha256 (downloaded wheel) | `266033a0a438b2fccc64b5ffec7a7929789108a4962347d6d57dfeb1cfd55897` (match, on both machines) |
| Installed files | All 2744 hashed RECORD entries of the wheel are in the installed RECORD and match the installed files' sha256: 0 mismatched, 0 missing (Windows and Linux) |
| `pip show` (`uv pip show`) | `Name: openminion`, `Version: 0.1.13`, `Location: <VENV>\Lib\site-packages` (Windows), `<VENV>/lib/python3.12/site-packages` (Linux) |
| Imported module | Every probe prints `openminion.__file__` (under `<VENV>`'s `site-packages`) and `__version__` = `0.1.13`. Probes run from empty directories outside any checkout, with `python -I` |
| Windows | Native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4 |
| Linux | Ubuntu 24.04.5 LTS GitHub-hosted runner ([AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner), preset `none`: the checkout is the runner repo itself, not openminion), CPython 3.12.15, SQLite 3.45.1; PostgreSQL 16.15 (`postgres:16`), SQLAlchemy 2.1.6 and psycopg 3.3.6 where a run used PostgreSQL (#401, #420) |
| 0.1.12 (controls and upgrade seeds) | `openminion==0.1.12` from PyPI in a separate venv: wheel sha256 `0d9c241e8575a42d54169d50a3db112c23f9980737a593d914ce30090ca74812` matches PyPI, and all 2744 RECORD entries match (Windows and Linux) |

Details are in [`provenance.json`](provenance.json), [`probes/provenance.py`](probes/provenance.py), [`windows/provenance/`](windows/provenance) and [`linux/provenance/`](linux/provenance) (provenance-only runner run [40449841350](https://github.com/AliceMessi/linux-runner/actions/runs/40449841350)). Every runner run repeats the check in its own `provenance-linux.json`, and its `probe-sha256.txt` records the probe files as run; they are byte-identical to the files in `probes/`.

## Release source contains each fix

Checked first with [`probes/release_source.py`](probes/release_source.py) (output [`release-source/release_source.json`](release-source/release_source.json)) and the GitHub API ([`release-source/github-api.txt`](release-source/github-api.txt)):

- The GitHub release `v0.1.13` was published 2026-10-27T04:22:11Z. The annotated tag `5f8253ec` points to commit `03c57902` ("Merge pull request #443 from openminion/dev", "Promote OpenMinion 0.1.13").
- For each of the 5 fixes, both the PR's merge commit and its reviewed head commit are ancestors of `03c57902`: `git merge-base --is-ancestor`, and `behind_by: 0` from the GitHub compare API.
- The published archives equal the tag source. Every one of the 2738 `openminion/` files in the PyPI wheel is byte-identical to `src/openminion/` at the tag. Every one of the 2743 tag-tracked files in the PyPI sdist (`openminion-0.1.13.tar.gz`, sha256 `e72e08db…c246`) is byte-identical to the tag; the other 8 sdist files are build metadata (`PKG-INFO`, `setup.cfg`, `src/openminion.egg-info/*`).
- Every source file each PR touched is in the wheel and the sdist and equals the tag. The only files changed after the PRs' heads are the CHANGELOG (all five) and, for #428, a lint baseline; they are listed under `files_changed_after_pr_head`.

| Issue | PR | Merge commit | Reviewed head |
|---|---|---|---|
| #412 | #428 | `773491f2` | `962005ab` |
| #413 | #431 | `a213dcd2` | `e6adb144` |
| #420 | #429 | `7af275db` | `2f0112a8` |
| #401 | #433 | `ae6b45fd` | `5a97f581` |
| #419 | #430 | `dfa1e0be` | `8d71ac50` |

## Results

Every check below ran on the published 0.1.13, and nothing failed on it.

| Issue | Maintainer's checklist (published package) | Result | Evidence |
|---|---|---|---|
| #412 | Real storagectl migrate/backup/restore onto an existing target and a new path: all snapshot tables, rows, identity pragmas and marker contents retained, no restore temp file; native Windows replacement with GC unable to hide an open CLI-owned connection; a missing snapshot fails, preserves an existing target, creates no database, keeps progress/JSON behaviour | **PASS**: 64/64 on Windows and 64/64 on Linux | [`windows/412`](windows/412), [`linux/412`](linux/412) |
| #413 | Real `memctl inspect` without `--scope`, empty and whitespace, JSON and human: all-scope stats and default identifiers; a valid scope filters and both renderers show the same normalized scope; keyed history through the audited SQLite store; an invalid scope gives the CLI error, exit 1, no traceback, no service opened; command-owned closes and borrowed ownership on success and failure | **PASS**: 36/36 on Windows and 36/36 on Linux | [`windows/413`](windows/413), [`linux/413`](linux/413) |
| #420 | A real memory-consolidation cron run with a pending terminal capture intent: brain session API, capture identity, recovered once, scheduled turn submitted; authorization denial, unsupported capture and no-pending controls with the turn still running; events recorded | **PASS**: 22/22 on Windows and 32/32 on Linux (adds a PostgreSQL memory store case) | [`windows/420`](windows/420), [`linux/420`](linux/420) |
| #401 | Real configured `memctl gc` on PostgreSQL through its audited store: exit 0, soft-deleted records and candidates purged, retained rows and evidence intact; bare/audited PostgreSQL and SQLite controls with the existing error behaviour | **PASS**: 34/34 on Linux (16 PostgreSQL checks), 18/18 SQLite controls on Windows | [`linux/401`](linux/401), [`windows/401`](windows/401) |
| #419 | First-entry and continuation text enforce the typed freshness obligations; a same-turn lookup keeps its evidence through judge/guard continuations, with tool-continuation and no-obligation controls; a new turn does not reuse evidence; stored working state through the runtime SQLite and local-session owners, old-payload defaults; coordinated upgrade with matching backups for rollback | **PASS**: `probe_419.py` 35/35 and `probe_419_upgrade.py` 15/15, on Windows and on Linux | [`windows/419`](windows/419), [`linux/419`](linux/419) |

The comment posted on each issue lists every checklist item with what ran and what was observed. The `.json` outputs hold every check with its observed values; the `.log` files hold the console output.

### #412 (`probes/probe_412.py`, `probe_412_lib.py`, `probe_412_child.py`, `run_412_windows.sh`, `steps_412.sh`)

- The package has no `storagectl` console script. storagectl is `python -m openminion.modules.storage` (argparse prog `storagectl`), run as its own process.
- Eight cases: existing target or new path, plain or in a GC-off child with `--progress`, from the real snapshot or from a missing one. The GC-off child calls `gc.disable()` before importing openminion, counts `gc.collect` (0), lists the sqlite3 connections open on the target at the replace, and renames, replaces and deletes the target right after the command.
- The restored target is byte-identical to the snapshot: 28 tables, 96 schema objects, 25 rows, `application_id` 1330446337, `alembic_version` `0005_cron_scheduler_control`, marker rows, no `*.restore.tmp`. At the replace, 0 connections are open on the target.
- Linux ran without PostgreSQL: storagectl backup and restore operate on SQLite files.
- Runner run [40450149250](https://github.com/AliceMessi/linux-runner/actions/runs/40450149250).

### #413, #401 (`probes/probe_413.py`, `probe_401.py`, `probe_401_lib.py`, `run_413_401_windows.sh`, `steps_413_401.sh`)

- #413's CLI behaviour uses the installed `memctl` console script. Only the closure and ownership item runs in process (typer `CliRunner`, GC disabled, `gc.collect` called 0 times), because it needs counters.
- #401 runs the installed `memctl gc --json` with a typed `MEMCTL_CONFIG` and no `--db`, so memctl builds its own `AuditedMemoryStore(PostgresMemoryStore)`. Rows and candidates are read server-side before and after (6 to 4 rows, 4 to 2 candidates); artifact edges come from the real artifact index. A `BEFORE DELETE` trigger shows the existing error behaviour on both backends.
- Runner run [40450823501](https://github.com/AliceMessi/linux-runner/actions/runs/40450823501). Run 40450437475 was superseded (two "same output" checks in probe_413 passed vacuously on 0.1.12; they were tightened) and is not used.

### #420 (`probes/probe_420.py`, `probe_420_lib.py`, `steps_420.sh`)

- A real `APIRuntime` with the SDK's `openai` provider against a scripted OpenAI-compatible server on 127.0.0.1. The job is created by the installed `task.consolidate_memory` handler, queued with `run_task_now`, and run by the daemon's own `attach_cron_scheduler` (the real `CronScheduler` with `CronTurnExecutor`). Each case runs the job twice.
- From the probe, not production: the pending intent comes from a `StoreWriteError` injected once into the turn's capture bundle write (as upstream's regression test does), the job handler is called directly, and the denial comes from an identity profile the probe writes.
- Runner run [40450357554](https://github.com/AliceMessi/linux-runner/actions/runs/40450357554).

### #419 (`probes/probe_419.py`, `probe_419_lib.py`, `probe_419_upgrade.py`, `run_419_windows.sh`, `steps_419.sh`)

- Proof boundary: the real installed `APIRuntime` and SDK `Agent` with an `@tool lookup_order`, against a scripted OpenAI-compatible server on 127.0.0.1; a socket guard refuses non-loopback connections (none was attempted). The script stands in for the model only.
- Working state is read raw and through both owners: `SessctlAdapter` over the runtime's `brain/sessions.db`, and `LocalSessionStore` (the installed `brainctl` in local mode, plus SDK states saved with `save_state`).
- The coordinated upgrade follows `docs/runtime-database-upgrades.md` at `v0.1.13` (the document is not in the wheel): 0.1.12 writers, stop, backup, 0.1.13 continues both sessions, 0.1.12 rejects the newer state (`extra_forbidden`, shown on a scratch copy only), rollback with the matching backup and 0.1.12 continues. Mixed old/new readers were not run and are not claimed.
- Runner run [40450706921](https://github.com/AliceMessi/linux-runner/actions/runs/40450706921).

## Controls on the published 0.1.12

Every probe also ran against `openminion==0.1.12` from PyPI in a separate venv, to show that it detects the defect the release fixes:

| Issue | 0.1.12 result | What fails |
|---|---|---|
| #412 | 36 of 64 fail (Windows), 18 of 64 (Linux) | Windows: every restore fails with `PermissionError [WinError 5]` at the replace, with the `StorageEngine`'s record-store connection open on the target; `*.restore.tmp` left behind. Linux: a restore to a new path prints `ok: true` but leaves 3 of 28 tables and 0 rows. A missing snapshot creates a database at the new path |
| #413 | 23 of 36 fail (Windows and Linux, the same checks) | no scope, `""` and `"   "` exit 1 with sophiagraph's `InvalidArgumentError` traceback; `"  agent:a  "` is not normalized; an invalid scope gives a traceback and opens the service first |
| #420 | 10 of 22 fail (Windows), 17 of 32 (Linux) | recovery raises `TypeError: BrainBridgeService.extract_memory_capture_candidates() takes 1 positional argument but 5 were given`; the run is stored `failed` and the scheduled turn is never submitted |
| #401 | 8 of 34 fail (Linux, all PostgreSQL through the audited store); 18/18 SQLite checks pass on Windows | `memctl gc` exits 1 with `'PostgresMemoryStore' object has no attribute '_connect'` after the retention pass; all 6 rows and 4 candidates remain on the server |
| #419 | 27 of 35 fail (Windows and Linux) | entry and continuation text return unverified answers; the respond route never calls the verifier; no stored working state has `turn_evidence` or `turn_evidence_trace_id` |

The control outputs are in each issue's `windows/<issue>/control-0.1.12/` and in `linux/<issue>/` (files with `_0112` in the name).

## Observations outside the acceptance checks

- **No `storagectl` console script, and `python -m openminion.modules.storage` exits 0 when `main()` returns nonzero** (from #412). Its `__main__` calls `main()` without `raise SystemExit(main())`. On 0.1.13, `pool-stats` without a PostgreSQL URL prints `ok: false`, `main()` returns 2, and the process exits 0 (a one-off manual check, not in the probe output). `restore` is not affected: its failures raise and exit 1.
- **`memctl stats --scope "  agent:a  "` is not normalized** (from #413): it reports 0 records, while `memctl inspect` now normalizes the same value and reports 2.
- **`memctl gc` prints a full traceback for an unexpected database error** (from #401): `cmd_gc` catches only `AttributeError` and `TypeError`. Unchanged from 0.1.12.
- **An identity hard constraint against memory writes blocks only capture recovery** (from #420): the turns' own terminal capture still goes to the bundle write. Unchanged from 0.1.12; it may be intended.
- **Unsupported capture intents are extracted again on every consolidation run** (from #420), one provider request per pending intent, and the pending set grows by each scheduled turn's own unsupported intent. This follows from #397's design.
- **The first working state 0.1.13 writes after the upgrade has neither new field** (from #419). It is the brain bridge's turn-start reset (`services/brain/post_execution/reset.py`, `_write_working_state_inline`), which rewrites the stored 0.1.12 dict. Every later row has both fields. Harmless, but the upgrade guide's "write both fields in every working state they save" is not literally true for that row.
- **`openminion storage backup --sqlite` refuses 9 of a runtime home's 16 SQLite files** (`application_id mismatch for module 'storage'`), from #419's upgrade. It backs up `state.db` and `brain/sessions.db`, where the working state lives; the other files were snapshotted with sqlite3's backup API. The guide does not say how to back them up.
- **On Windows, `LocalSessionStore` cannot save the SDK's conversation session ids** (`<sid>::conv:<id>`: `:` is not valid in a directory name, `WinError 123`), from #419. The runtime never uses the local store.
- **Windows error text is localized.** The Windows machine reports `WinError` messages in its OS language (Italian), so some control logs show them that way.

## Files

- `provenance.json`: the PyPI metadata, wheel hash comparison and RECORD check for 0.1.13 and 0.1.12 on Windows and Linux, plus the release-source summary.
- `release-source/`: `release_source.json` and `.log` (ancestry of each fix's commits, archives against the tag), `prs.json` (each PR's merge commit, head and files from the GitHub API, the script's input) and `github-api.txt`.
- `probes/`: the scripts as run.
  - `_common.py`: banner, checks, redaction, the echo runtime without repo fixtures, and `sqlite3.connect` tracking.
  - `provenance.py`, `release_source.py`, `linux_setup.sh`, `steps_provenance.sh`.
  - The `probe_*.py` probes and their helpers; `run_*_windows.sh` and `steps_*.sh` for Windows and the runner. The runner checkout is the runner repo itself, so no openminion source is on the runner.
- `windows/<issue>/`: native Windows outputs, `probe-sha256.txt` (the sha256 of each probe file as run) and `control-0.1.12/`. `windows/provenance/`: the Windows provenance runs.
- `linux/<issue>/`: the runner outputs per issue, with `run-id.txt`, `os.txt`, `pip-show*.txt`, `provenance-linux.json` and `probe-sha256.txt`. Where a run used no PostgreSQL, its `postgres-version.txt` says so (`No such container: pg`).
- `linux/provenance/`: the provenance-only runner run.

Runner runs on [AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner/actions):

| Run | Content |
|---|---|
| [40449841350](https://github.com/AliceMessi/linux-runner/actions/runs/40449841350) | provenance only (0.1.13 and 0.1.12) |
| [40450149250](https://github.com/AliceMessi/linux-runner/actions/runs/40450149250) | #412 |
| [40450823501](https://github.com/AliceMessi/linux-runner/actions/runs/40450823501) | #413, #401 |
| [40450357554](https://github.com/AliceMessi/linux-runner/actions/runs/40450357554) | #420 |
| [40450706921](https://github.com/AliceMessi/linux-runner/actions/runs/40450706921) | #419 |

The outputs contain no keys, credentials or personal data. The runner's fixture PostgreSQL URL is never printed. Local paths are replaced with `<VENV>`, `<VENV0112>`, `<WORK>`, `<TMP>`, `<RUNNER_TEMP>` and `<USER_HOME>`.
