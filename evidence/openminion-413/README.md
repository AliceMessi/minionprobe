# OpenMinion #413: `memctl inspect` with an omitted, empty, valid or invalid scope

Evidence for the fix PR of [#413](https://github.com/openminion/openminion/issues/413) (branch `fix/413-memctl-inspect-scope`, head `abb9020d`, on `dev` @ `8c0568f7`, which includes #416 and #418). `memctl inspect` without a scope, or with a blank one, now inspects every scope with the default identifiers; an invalid non-empty scope is reported as `Error: invalid scope: ...` with exit code 1 and no traceback, as `memctl search` and `memctl list` report it. The original report and its probe (`probe_inspect_scope.py`, sha256 `01856327...be53`, unchanged) are in [`openminion-memctl-inspect-scope`](../openminion-memctl-inspect-scope).

Files:
- `run-linux-gates.log`, from `run413.sh`: Ubuntu 24.04 GitHub-hosted runner with PostgreSQL 16. On the branch head and on `dev` sources (`git checkout 8c0568f7 -- src`):
  - the focused memctl tests: 46 passed on the head; on `dev` sources 8 failed (every no-scope and invalid-scope case), 38 passed;
  - `tests/memory tests/integration/test_operability.py` with the `make test-ci` markers: 857 passed on the head; the same 8 failed, 849 passed on `dev` sources;
  - `tests/memory/test_operability_postgres.py` with `-m postgres`: 2 passed on both trees;
  - the issue's probe: exit 0 without a scope and a clean `Error:` line for an invalid scope on the head; the sophiagraph traceback on `dev` sources;
  - `ruff check .`, `make format-check`, `make workflow-check`, `make lint`, `make test-critical`.
- `windows.log`, from `windows413.sh`: native Windows 11, CPython 3.11.11: the focused tests and the issue's probe on both trees, with the same results.
- `run-linux-testci.log`, from `testci.sh`: the full `make test-ci` on the branch head. Run 40298559877: 1 failed, 20362 passed, 29 skipped, 1361 deselected. The failure is `test_unreachable_db_raises_with_redacted_url`, which times out (60 s, unroutable `192.0.2.1`) only when the `postgres` extra (psycopg) is installed, on `dev` too (see `../openminion-408/run-linux-dev-pg-timeout-test.log`).
