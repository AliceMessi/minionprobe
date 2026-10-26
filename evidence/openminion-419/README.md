# OpenMinion #419: text entry answers go through the freshness finalization

Evidence for the fix PR of [#419](https://github.com/openminion/openminion/issues/419) (branch `fix/419-text-answer-freshness-finalization`, head `7e8eba5e`, on `dev` @ `8c0568f7`, which includes #422). The respond route now finalizes an entry step's text answer with `final_close_message`, the owner that finalizes a judge's closing answer: the answer is kept when its evidence meets the turn's obligations and becomes the existing freshness-failure message when it does not. The original report and its probe (`probe_text_answer_freshness.py`, sha256 `452ed05e...29ea`, unchanged) are in [`openminion-text-answer-freshness`](../openminion-text-answer-freshness).

Files:
- `run-linux-gates.log`, from `run419.sh`: Ubuntu 24.04 GitHub-hosted runner. On the branch head and on `dev` sources (`git checkout 8c0568f7 -- src`):
  - the focused tests (the new SDK tests and #422's): 21 passed on the head; on `dev` sources 4 failed (the two failure tests, and the passing and no-obligation controls on their assertion that the verifier ran), 17 passed;
  - the related freshness and entry tests: 469 passed on both trees;
  - `tests/brain tests/api tests/modules/brain` with the `make test-ci` markers: 4914 passed on the head; the same 4 failed, 4910 passed on `dev` sources;
  - the issue's probe: T0, T1 and C end with the freshness-failure message on the head and with the unchecked text on `dev` sources; no non-loopback connection;
  - `ruff check .`, `make format-check`, `make workflow-check`, `make lint`, `make test-critical`.
- `windows.log`, from `windows419.sh`: native Windows 11, CPython 3.11.11: the focused tests plus the freshness enforcement and routing tests (39 passed on the head; the same 4 failed on `dev` sources) and the issue's probe on both trees.
- `run-linux-testci.log`, from `testci.sh`: the full `make test-ci` on the branch head. Run 40297718824: 1 failed, 20357 passed, 29 skipped, 1361 deselected. The failure is `test_unreachable_db_raises_with_redacted_url`, which times out (60 s, unroutable `192.0.2.1`) only when the `postgres` extra (psycopg) is installed, on `dev` too (see `../openminion-408/run-linux-dev-pg-timeout-test.log`).

Round 2 (review of `7e8eba5e`: same-turn evidence for a continuation's text answer, head `1d4d4d15` on `dev` @ `773491f2`): [`round2/`](round2).
