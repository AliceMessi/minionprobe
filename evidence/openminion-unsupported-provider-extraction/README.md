# OpenMinion: capture recovery on a backend without atomic capture, bounded runs

The separate report requested when closing [openminion#420](https://github.com/openminion/openminion/issues/420#issuecomment-6441322163), against the #397 capability contract. In the 0.1.13 verification of #420 ([`openminion-0.1.13-verification`](../openminion-0.1.13-verification), case U), each consolidation run extracted again for every unsupported intent, and the pending set grew by each scheduled turn's own intent. This folder counts that over a fixed number of runs, with nothing injected.

## What ran

- Native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4, 27 Oct 2026.
- Two fresh `uv` venvs:
  - `openminion==0.1.13` from PyPI ([`windows/0.1.13`](windows/0.1.13));
  - `openminion @ git+https://github.com/openminion/openminion@68d623d10317a7cea13832f8dbd51fc98a871aa5`, current `dev` ([`windows/dev-68d623d1`](windows/dev-68d623d1); `direct_url.json` records the commit). Its version string is 0.1.13.
- [`probes/probe_unsupported_extraction.py`](probes/probe_unsupported_extraction.py) with 14 runs, run with `python -I -X utf8` from an empty directory outside any checkout. No repo test fixtures.
- [`probes/probe_420_lib.py`](probes/probe_420_lib.py) and [`probes/_common.py`](probes/_common.py) are byte-identical copies of the helpers in [`openminion-0.1.13-verification/probes`](../openminion-0.1.13-verification/probes) (see [`windows/probe-sha256.txt`](windows/probe-sha256.txt)).
- Each case builds a real `APIRuntime` with one agent, `openminion`, whose `openai` provider points at a scripted OpenAI-compatible server on 127.0.0.1. Then:
  1. one SDK turn, "Please remember that I prefer tabs over spaces for indentation.", in session `capture`;
  2. the installed `task.consolidate_memory` handler creates the consolidation job with `{"interval_hours": 12}`, so `batch_limit` is the package default (12), through the daemon's `TaskManager` over the daemon's cron store;
  3. the daemon's own `attach_cron_scheduler(runtime=...)` starts the real `CronScheduler`;
  4. `TaskManager.run_task_now` queues 14 runs, one after another. The probe waits for each `cron.run.finished`.
- Case U uses the bundled `reference-sqlite` external backend (`SophiaGraphSqliteStore` behind `BackendMemoryStoreAdapter`). Case S (control) uses the built-in store (`AuditedMemoryStore` over `SQLiteMemoryStore`).
- Nothing is injected. The probe wraps runtime objects with recorders that call the original and never change a result: recovery, the capture extractor, the memctl bundle write, `submit_turn` (to read the request meta), the brain's `capture_is_excluded` call, and the scheduler's cron events.

## Results

0.1.13 and `dev` give the same table and totals: 10/10 checks pass on each.

Case U, per run (k = run number):

| Run | Pending before | Scanned / recovered / pending | Extraction requests in recovery | Extraction requests by the scheduled turn | Pending after |
|---|---|---|---|---|---|
| 1 | 1 | 1 / 0 / 1 | 1 | 1 | 2 |
| 2 | 2 | 2 / 0 / 2 | 2 | 1 | 3 |
| 3 | 3 | 3 / 0 / 3 | 3 | 1 | 4 |
| 4 | 4 | 4 / 0 / 4 | 4 | 1 | 5 |
| 5 | 5 | 5 / 0 / 5 | 5 | 1 | 6 |
| 6 | 6 | 6 / 0 / 6 | 6 | 1 | 7 |
| 7 | 7 | 7 / 0 / 7 | 7 | 1 | 8 |
| 8 | 8 | 8 / 0 / 8 | 8 | 1 | 9 |
| 9 | 9 | 9 / 0 / 9 | 9 | 1 | 10 |
| 10 | 10 | 10 / 0 / 10 | 10 | 1 | 11 |
| 11 | 11 | 11 / 0 / 11 | 11 | 1 | 12 |
| 12 | 12 | 12 / 0 / 12 | 12 | 1 | 13 |
| 13 | 13 | 12 / 0 / 12 | 12 | 1 | 14 |
| 14 | 14 | 12 / 0 / 12 | 12 | 1 | 15 |
| Total | | 102 / 0 / 102 | 102 | 14 | |

- Every bundle write in recovery raised `CaptureRecoveryUnsupportedError: memory backend SophiaGraphSqliteStore does not support atomic capture bundles`. No other error. No `memory.capture.result` was written.
- `SophiaGraphSqliteStore` had no callable `apply_capture_bundle` before the first turn.
- The user turn's root turn has one more `brain.auto_fact_extraction.started` event per run in the brain session API: 14 after 14 runs.
- Runs 13 and 14 scan the same 12 intents: the user turn's and those of the scheduled turns of runs 1 to 11. The pending intents of runs 12 and 13 are not scanned.
- Each scheduled turn makes one extraction request. Its own bundle (no candidates) raises the same error, and its intent is `pending`, not `excluded`.
- The scheduled turn's request meta at `submit_turn` has `memory_consolidation_job: "true"`. The brain's `capture_is_excluded` gets message metadata without that key (with `cron_job_id`, `cron_run_id`, `scheduled_for`) and returns `False`. The same holds in case S.
- Provider requests in total: U 162 (117 extraction, 15 decide, 30 freshness); S 60 (15 extraction, 15 decide, 30 freshness).

Case S (control): `SQLiteMemoryStore.apply_capture_bundle` is callable. All 14 runs scan 0, call no extractor and make no provider request inside recovery. Each scheduled turn's own bundle commits `succeeded_no_output`. Nothing stays pending. All runs finish in both cases.

## Files

- `windows/<build>/probe_unsupported_extraction.json`: the per-run table, the totals, the checks and, per case and run, the recovery results, which intents were extracted, the bundle writes, the scheduled turn's intent, the request meta and the `capture_is_excluded` inputs.
- `windows/<build>/probe_unsupported_extraction.log`: the console output, with one line per run.
- `windows/<build>/pip-list.txt`, `pip-show.txt` (and `direct_url.json` for `dev`).
- Paths are sanitized (`<VENV>`, `<WORK>`).

## Limits

- One user turn. The runs are queued back to back, not on the 12-hour interval.
- The job is created by calling the tool's handler directly, as in the #420 verification. The memory config is registered with `ConfigManager.register("memory", ...)`.
- Native Windows only. Not run on Linux or PostgreSQL. Other backends without atomic capture were not run.
- No fix and no code change.
