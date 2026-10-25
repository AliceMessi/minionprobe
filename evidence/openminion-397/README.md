# OpenMinion #397: external backends without atomic capture are reported as unsupported

Evidence for the fix PR of [#397](https://github.com/openminion/openminion/issues/397) (branch `fix/397-external-capture-unsupported`, head `62a8819c`, on `dev` @ `b670bbb6`). `BackendMemoryStoreAdapter.apply_capture_bundle` now raises the existing `CaptureRecoveryUnsupportedError` for a backend without the method instead of `NotImplementedError`; a backend that implements it is called as before. The original report and its probe (`probe_external_capture.py`, sha256 `3cfac3d4...6550`, unchanged) are in [`openminion-external-capture`](../openminion-external-capture).

Files:
- `run-linux-focus.log`, from `run397.sh`: Ubuntu 24.04 GitHub-hosted runner with PostgreSQL 16 (AliceMessi/linux-runner run 40205734363). On the branch head and on `dev` sources (`git checkout b670bbb6 -- src`):
  - the focused tests: 50 passed on the head; on `dev` sources 2 failed (the adapter test and the `reference-sqlite` SDK test, `NotImplementedError`), 48 passed;
  - the related capture, gateway memory and memory assembly tests: 130 passed on both; the PostgreSQL capture bundle and conformance tests: 19 passed on both;
  - the issue's probe: both `reference-sqlite` turns complete on the head and raise on `dev` sources;
  - a merge with PR #398's head `6f1d29bc` (no conflict): the focused tests, #398's tests and #398's PostgreSQL memory runtime tests pass, and the probe gives the same lines;
  - `ruff check .`, `make format-check`, `make workflow-check`, `make lint`, `make test-critical`.
- `windows.log`: native Windows 11, CPython 3.11.11: the focused tests and the issue's probe on the head and on `dev` sources.
- `run-linux-testci.log`: the full `make test-ci` on the branch head (run 40205734898): 1 failed, 20262 passed, 29 skipped, 1333 deselected. The failure is `test_unreachable_db_raises_with_redacted_url`, which times out only when the `postgres` extra (psycopg) is installed, on `dev` too (see `../openminion-408/run-linux-dev-pg-timeout-test.log`).

The consolidation cron recovery failure found while testing is in [`openminion-cron-capture-recovery`](../openminion-cron-capture-recovery) (#420).
