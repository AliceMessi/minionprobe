# OpenMinion #396: case-sensitive `run.` prefix in the two run readers

Evidence for the fix PR of [#396](https://github.com/openminion/openminion/issues/396) (branch `fix/396-run-reader-case-sensitive-prefix`, head `f60cc798`, on `dev` @ `b670bbb6`). `list_run_events` and `summarize_recent_runs` now select run events with `SUBSTR(event_type, 1, 4) = 'run.'`, the equality `summarize_thread_events` already uses, instead of `LIKE 'run.%'`. The original report and its probe (`probe_run_event_type_case.py`, sha256 `d5f914fb...807c`, unchanged) are in [`openminion-run-event-type-case`](../openminion-run-event-type-case).

Files:
- `run-linux-focus.log`, from `run396.sh`: Ubuntu 24.04 GitHub-hosted runner with PostgreSQL 16 (AliceMessi/linux-runner run 40203123786). On the branch head and on `dev` sources (`git checkout b670bbb6 -- src`):
  - the focused tests: 131 passed on the head; on `dev` sources 3 failed (the new case scenario, the payload-variant scenario and the event-facts upgrade scenario, whose run-event oracle no longer uses the store's `LIKE`), 128 passed;
  - `tests/integration/test_runtime_storage_postgres.py -m postgres`: 22 passed on both, including the new case test;
  - the related run API and run status tests: 97 passed on both;
  - the issue's probe: on the head the SQLite lines equal the PostgreSQL lines; on `dev` sources they are the report's;
  - `plans_396.py`: the exact SQL each reader sends, with `EXPLAIN QUERY PLAN` on SQLite and `EXPLAIN ANALYZE` on PostgreSQL, 2 sessions of 2,400 events;
  - `ruff check .`, `make format-check`, `make workflow-check`, `make lint`, `make test-critical`.
  - The header's PostgreSQL version query fails (`lc_collate` is not a setting on PostgreSQL 16); it only affects that header line.
- `run-linux-pgplans.log`, from `run396b.sh` and `plans_396_pg.py` (run 40203880856): PostgreSQL plans with `ANALYZE, BUFFERS` and median timings for 48,000 events, as 40 sessions x 100 turns and as 8 sessions x 500 turns, scan limits 200 and 2,000, on the head and on `dev` sources. The SQLite plans are unchanged; the PostgreSQL plan of the recent-runs window changes, as summarized in the PR's "For your decision".
- `windows.log`: native Windows 11, CPython 3.11.11 (no PostgreSQL): the focused tests and the issue's probe on the head and on `dev` sources, and Ruff on the changed files.
- `run-linux-testci.log`: the full `make test-ci` on the branch head (run 40203124053): 1 failed, 20256 passed, 29 skipped, 1334 deselected. The failure is `test_unreachable_db_raises_with_redacted_url`, which times out only when the `postgres` extra (psycopg) is installed, on `dev` too (see `../openminion-408/run-linux-dev-pg-timeout-test.log`).
