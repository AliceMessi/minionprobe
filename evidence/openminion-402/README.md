# OpenMinion #402: `memctl inspect` history summary with the audited SQLite store

Evidence for the fix PR of [#402](https://github.com/openminion/openminion/issues/402) (branch `fix/402-inspect-history-audited-store`, head `4b5df8dc`, on `dev` @ `b670bbb6`). `build_inspect_payload` now resolves `AuditedMemoryStore` before its SQLite-only history check, as `compute_stats` does. The original report and its probe are in [`openminion-diagnostics-history`](../openminion-diagnostics-history).

Files:
- `run-linux-focus.log`, from `run402.sh`: Ubuntu 24.04 GitHub-hosted runner with PostgreSQL 16 (AliceMessi/linux-runner run 40199835696). On the branch head and on `dev` sources (`git checkout b670bbb6 -- src`):
  - `tests/memory/test_operability.py`: 9 passed on the head; on `dev` sources the history test fails with `{'json': None, 'human': []}` (the count is in `run-linux-dev-count.log`, run 40200167164: 1 failed, 8 passed);
  - the related operability, PostgreSQL operability, memory blocks CLI and audit trail tests: 30 passed on both;
  - the issue's probe `probe_inspect_history.py`;
  - `pg_inspect_history.py`: PostgreSQL still has no history summary, bare or behind the wrapper, on both trees;
  - `probe_inspect_scope.py`: `memctl inspect` without `--scope` still fails on both trees; reported separately as [#413](https://github.com/openminion/openminion/issues/413) (evidence [`openminion-memctl-inspect-scope`](../openminion-memctl-inspect-scope));
  - `ruff check .`, `make format-check`, `make workflow-check`, `make lint`, `make test-critical`.
- `windows.log`: native Windows 11, CPython 3.11.11 (no PostgreSQL): the focused and related tests and the issue's probe on the head and on a clean worktree of `b670bbb6`.
- `run-linux-testci.log`: the full `make test-ci` on the branch head (run 40199852396): 1 failed, 20259 passed, 29 skipped, 1333 deselected. The failure is the environmental `test_unreachable_db_raises_with_redacted_url` timeout, which also fails on `dev` with the `postgres` extra installed (see `../openminion-408/run-linux-dev-pg-timeout-test.log`).
