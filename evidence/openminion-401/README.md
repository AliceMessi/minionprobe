# OpenMinion #401: `memctl gc` purges with a PostgreSQL memory store

Evidence for the fix PR of [#401](https://github.com/openminion/openminion/issues/401) (branch `fix/401-memctl-gc-purge-wrapped-store`, head `375db861`, on `dev` @ `8c0568f7`, which includes #399). `purge_soft_deleted` now resolves the `AuditedMemoryStore` that memctl builds to the store it wraps, with #399's `backing_store`, which moves into the purge module. The original report and its probe (`probe_gc_purge_pg.py`, sha256 `adeeb6a0...cb4d`, unchanged) are in [`openminion-gc-purge-pg`](../openminion-gc-purge-pg).

Files:
- `run-linux-gates.log`, from `run401.sh`: Ubuntu 24.04 GitHub-hosted runner with PostgreSQL 16. On the branch head and on `dev` sources (`git checkout 8c0568f7 -- src`):
  - the focused SQLite gc tests: 46 passed on both trees (the SQLite controls);
  - `tests/memory tests/services/agent` with the `make test-ci` markers: 1317 passed, 2 skipped on both trees;
  - the PostgreSQL gc, evidence, operability and conformance tests with `-m postgres`: 41 passed on the head; on `dev` sources 2 failed (the purge scenario through the audited store and the typed-config `memctl gc`, both `'PostgresMemoryStore' object has no attribute '_connect'`), 39 passed;
  - the issue's probe: on the head the PostgreSQL `memctl gc` exits 0 and purges; on `dev` sources it exits 1 with the soft-deleted row left;
  - `ruff check .`, `make format-check`, `make workflow-check`, `make lint`, `make test-critical`.
- `windows.log`, from `windows401.sh`: native Windows 11, CPython 3.11.11, no PostgreSQL: the focused SQLite tests (46 passed on both trees) and the probe's SQLite control.
- `run-linux-testci.log`, from `testci.sh`: the full `make test-ci` on the branch head. Run 40301605773: 1 failed, 20355 passed, 29 skipped, 1364 deselected (the new PostgreSQL cases are deselected by the `postgres` marker and run in the gates log). The failure is `test_unreachable_db_raises_with_redacted_url`, which times out (60 s, unroutable `192.0.2.1`) only when the `postgres` extra (psycopg) is installed, on `dev` too (see `../openminion-408/run-linux-dev-pg-timeout-test.log`).
