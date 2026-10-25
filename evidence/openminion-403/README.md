# OpenMinion #403: memctl closes the service, store and audit database it creates

Evidence for the fix PR of [#403](https://github.com/openminion/openminion/issues/403) (branch `fix/403-memctl-service-close`, head `6aa73f15`, on `dev` @ `b670bbb6`). `_get_service` now owns the store and audit sink it creates, and each memctl command passes its typer context so the service is closed through `ctx.call_on_close` when the command ends; outside a command the caller closes it. The original report and its probe (`probe_memctl_audit_sink.py`, sha256 `592c272d...2814`, unchanged) are in [`openminion-memctl-audit-sink`](../openminion-memctl-audit-sink).

Files:
- `run-linux-focus.log`, from `run403.sh`: Ubuntu 24.04 GitHub-hosted runner with PostgreSQL 16 (AliceMessi/linux-runner run 40204480739). On the branch head and on `dev` sources (`git checkout b670bbb6 -- src scripts/baselines`):
  - the focused tests: 41 passed on the head; on `dev` sources 8 failed (the new tests except the borrowed-service control), 33 passed;
  - `tests/memory/test_operability_postgres.py -m postgres`: 2 passed on both;
  - the related memctl tests: 60 passed on both;
  - the issue's probe: 0 open audit connections after the in-process command on the head, 1 on `dev` sources;
  - `ruff check .`, `make format-check`, `make workflow-check`, `make lint`, `make test-critical`.
- `windows.log`: native Windows 11, CPython 3.11.11, typer 0.27.3 (no PostgreSQL): the focused and related tests and the issue's probe on the head and on `dev` sources. On `dev` sources the 3 `TestCLIWriteCommands` tests and 5 `TestMemoryForgetCLI` tests fail with `WinError 32` on `test.audit.db`; on the head only `test_forget_emits_delete_event_to_audit_sink` still fails, because the test keeps its own `SQLiteMemoryAuditSink` open (disclosed in the PR). The localized Windows error text is trimmed.
- `run-linux-testci.log`: the full `make test-ci` on the branch head (run 40204480558): 1 failed, 20264 passed, 29 skipped, 1333 deselected. The failure is `test_unreachable_db_raises_with_redacted_url`, which times out only when the `postgres` extra (psycopg) is installed, on `dev` too (see `../openminion-408/run-linux-dev-pg-timeout-test.log`).
