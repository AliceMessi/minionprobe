# OpenMinion #412: `storagectl restore` without a StorageEngine on the target

Evidence for the fix PR of [#412](https://github.com/openminion/openminion/issues/412) (branch `fix/412-storagectl-restore-dispatch`, head `962005ab`, on `dev` @ `8c0568f7`). `storagectl` now dispatches `restore` before it builds `StorageEngine.from_paths(...)`, so its SQLite record store no longer holds a connection on the database file the restore replaces. The original report and its probe (`probe_storagectl_restore.py`, sha256 `ee295c7a...a7ee`, unchanged) are in [`openminion-storagectl-restore-target`](../openminion-storagectl-restore-target).

Files:
- `run-linux-gates.log`, from `run412.sh`: Ubuntu 24.04 GitHub-hosted runner with PostgreSQL 16. On the branch head and on `dev` sources (`git checkout 8c0568f7 -- src`):
  - the focused storagectl tests: 92 passed on the head; on `dev` sources 2 failed (the new-path restore and the new-path missing-snapshot test), 90 passed;
  - `tests/storage tests/cli` with the `make test-ci` markers: 1 failed, 2118 passed on the head; 3 failed, 2116 passed on `dev` sources. The failure on both is `test_unreachable_db_raises_with_redacted_url` (see below);
  - the issue's probe: case B (restore onto a new path) leaves the snapshot on the head and a database missing 22 of 25 tables on `dev` sources;
  - `ruff check .`, `make format-check`, `make workflow-check`, `make lint`, `make test-critical`.
- `windows.log`, from `windows412.sh`: native Windows 11, CPython 3.11.11, SQLite 3.47.1: the focused tests (92 passed on the head; 5 failed on `dev` sources, `PermissionError [WinError 5]` at the replace) and the issue's probe on both trees (cases A and B fail with `PermissionError` on `dev` sources and leave the snapshot on the head).
- `run-linux-testci.log`, from `testci.sh`: the full `make test-ci` on the branch head. Run 40297637767: 1 failed, 20356 passed, 29 skipped, 1361 deselected. The failure is `test_unreachable_db_raises_with_redacted_url`, which times out (60 s, unroutable `192.0.2.1`) only when the `postgres` extra (psycopg) is installed, on `dev` too (see `../openminion-408/run-linux-dev-pg-timeout-test.log`).
