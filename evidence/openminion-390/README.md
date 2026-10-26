# OpenMinion #390: a failed runtime memory assembly build closes what it obtained

Evidence for the fix PR of [#390](https://github.com/openminion/openminion/issues/390) (branch `fix/390-memory-assembly-construction-cleanup`, head `e0e7b440`, on `dev` @ `8c0568f7`). `build_memory_v2_runtime_assembly` now tracks the artifact store it obtains and the one current owner of the memory store (raw store, then audited store, then memory service); when the build raises it closes both once and its own error propagates. The original report and its probe are in [`openminion-gaps-380`](../openminion-gaps-380); that probe assumed a memory store for the `external` provider, which #388 removed, so the regressions below replace it.

Files:
- `run-linux-gates.log`, from `run390.sh`: Ubuntu 24.04 GitHub-hosted runner with PostgreSQL 16. On the branch head and on `dev` sources (`git checkout 8c0568f7 -- src`):
  - the focused memory assembly and bootstrap tests: 77 passed on the head; on `dev` sources 8 failed (every failure-path test: nothing is closed), 69 passed;
  - `tests/services tests/runtime tests/memory/runtime tests/api` with the `make test-ci` markers: 2657 passed on the head; the same 8 failed, 2649 passed on `dev` sources;
  - `-m postgres tests/integration/test_runtime_memory_postgres.py tests/memory/test_memory_postgres_conformance.py`: 26 passed on the head; on `dev` sources the new build-failure test fails (the PostgreSQL engine is not disposed), 25 passed;
  - `ruff check .`, `make format-check`, `make workflow-check`, `make lint`, `make test-critical`.
- `windows.log`, from `windows390.sh`: native Windows 11, CPython 3.11.11: the focused tests, 77 passed on the head; the same 8 failed, 69 passed on `dev` sources.
- `run-linux-testci.log`, from `testci.sh`: the full `make test-ci` on the branch head. Run 40300334667: 1 failed, 20361 passed, 29 skipped, 1362 deselected (the new PostgreSQL test is deselected by the `postgres` marker and runs in the gates log). The failure is `test_unreachable_db_raises_with_redacted_url`, which times out (60 s, unroutable `192.0.2.1`) only when the `postgres` extra (psycopg) is installed, on `dev` too (see `../openminion-408/run-linux-dev-pg-timeout-test.log`).
