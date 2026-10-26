# OpenMinion #420: consolidation cron runs with pending capture intents

Evidence for the fix PR of [#420](https://github.com/openminion/openminion/issues/420) (branch `fix/420-cron-capture-extractor-adapter`, head `14f1fbe7`, on `dev` @ `8c0568f7`). The cron executor now passes a small adapter as the capture recovery's extractor: it keeps the recovery's positional callback, calls the agent service's keyword-only extractor by keyword and gives it the brain session API of the agent's runner. The original report and its probe (`probe_cron_capture_recovery.py`, sha256 `415e2755...1563`, unchanged) are in [`openminion-cron-capture-recovery`](../openminion-cron-capture-recovery).

Files:
- `probe_cron_recovery_turn.py` (sha256 `f9ec8e82...bead`): the issue's probe with two inputs changed so that the run can reach its scheduled turn, the daemon's `build_turn_request` as the request builder and a scheduler-shaped run (`run_id`, `due_at`), and with the recovery result recorded.
- `run-linux-gates.log`, from `run420.sh`: Ubuntu 24.04 GitHub-hosted runner with PostgreSQL 16. On the branch head and on `dev` sources (`git checkout 8c0568f7 -- src`):
  - the focused tests: 53 passed on the head; on `dev` sources 2 failed (the recovery success and unsupported-capture tests, `TypeError`), 51 passed;
  - `tests/runtime tests/memory/runtime tests/routines tests/services/runtime` with the `make test-ci` markers: 950 passed on the head; the same 2 failed, 948 passed on `dev` sources;
  - both probes: on the head, recovery completes and the turn probe's job returns its scheduled turn; on `dev` sources both raise the issue's `TypeError`;
  - `ruff check .`, `make format-check`, `make workflow-check`, `make lint`, `make test-critical`.
- `windows.log`, from `windows420.sh`: native Windows 11, CPython 3.11.11: the focused tests and both probes on the head and on `dev` sources, with the same results.
- `run-linux-testci.log`, from `testci.sh`: the full `make test-ci` on the branch head. Run 40297677661: 1 failed, 20356 passed, 29 skipped, 1361 deselected. The failure is `test_unreachable_db_raises_with_redacted_url`, which times out (60 s, unroutable `192.0.2.1`) only when the `postgres` extra (psycopg) is installed, on `dev` too (see `../openminion-408/run-linux-dev-pg-timeout-test.log`).
