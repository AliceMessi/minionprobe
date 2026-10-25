# OpenMinion #395: the turn keeps its freshness contract on continuation steps

Evidence for the fix PR of [#395](https://github.com/openminion/openminion/issues/395) (branch `fix/395-freshness-continuation-retention`, head `ad35d258`, on `dev` @ `b670bbb6`). `_apply_entry_freshness` now keeps the turn's freshness contract, obligations and diagnostics on a step with no new input and no valid typed contract, and emits `brain.freshness.entry_fallback` with reason `retained_turn_contract`. The original report and its probe (`probe_freshness_continuation.py`, sha256 `46f20259...c289`, unchanged) are in [`openminion-freshness-continuation`](../openminion-freshness-continuation); the #328 trace is in [`openminion-328-trace`](../openminion-328-trace).

Files:
- `run-linux-focus.log`, from `run395.sh`: Ubuntu 24.04 GitHub-hosted runner (AliceMessi/linux-runner run 40207481599). On the branch head and on `dev` sources (`git checkout b670bbb6 -- src`):
  - the focused tests: 19 passed on the head; on `dev` sources 11 failed (the 9 retention unit cases and the B and E SDK tests), 8 passed;
  - the related freshness, entry and closure tests: 469 passed on both;
  - the issue's probe: on the head B and E keep the exact-date contract on every continuation, and E's closes are all blocked until the turn ends on `llm_call_budget_exceeded`; on `dev` sources the report's lines;
  - `ruff check .`, `make format-check`, `make workflow-check`, `make lint`, `make test-critical`. An earlier run of the first head (`b3c7fb7e`, run 40206993080) failed `make lint` on the 1,000-line `max_file_loc` ceiling (`orchestration.py` at 1,002 lines); the final head compacts the new branch to 997 lines with the same behaviour.
- `windows.log`: native Windows 11, CPython 3.11.11: the focused and related tests and the probe's result lines on the head and on `dev` sources.
- `run-linux-testci.log`: the full `make test-ci` on the branch head (run 40207481369): 1 failed, 20271 passed, 29 skipped, 1333 deselected. The failure is `test_unreachable_db_raises_with_redacted_url`, which times out only when the `postgres` extra (psycopg) is installed, on `dev` too (see `../openminion-408/run-linux-dev-pg-timeout-test.log`).

The plain-text answer that still skips the freshness check is reported separately as [#419](https://github.com/openminion/openminion/issues/419), with its probe in [`openminion-text-answer-freshness`](../openminion-text-answer-freshness).
