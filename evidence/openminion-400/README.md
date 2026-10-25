# OpenMinion #400: PostgreSQL retention and purge of items with evidence

Evidence for the fix PR of [#400](https://github.com/openminion/openminion/issues/400) (branch `fix/400-pg-evidence-ref-decode`, head `529c76c6`, on `dev` @ `8b220e14`). The shared decoder `decode_evidence_ref_values` now returns an already decoded list (what PostgreSQL returns for the JSONB `evidence_json` column) as it is; stored JSON text, empty values and `None` take the existing path. The original report and its probe are in [`openminion-pg-evidence-refs`](../openminion-pg-evidence-refs).

Files:
- `run-linux-focus.log`, from `run400.sh`: Ubuntu 24.04 GitHub-hosted runner with PostgreSQL 16 (AliceMessi/linux-runner run 40190236823). On the branch head and on `dev` sources (`git checkout 8b220e14 -- src`):
  - the focused tests: 26 passed on the head; 9 failed (the 2 non-empty decoded-list unit tests and the 7 PostgreSQL cases, `JSONDecodeError`), 17 passed on `dev` sources;
  - the commands of the `postgres-memory-retrieval` CI job, with the new `tests/memory/test_gc_evidence_postgres.py`;
  - the related memory tests (1 failure on both trees, a PostgreSQL truth-maintenance test disclosed in #399);
  - the issue's probe `probe_pg_evidence_refs.py`: on the head the PostgreSQL output equals the SQLite control;
  - `compose399.py` on a merge with PR #399's head `fdcf4de7`: the four retention helpers through `AuditedMemoryStore(PostgresMemoryStore)` give the expected outcome with both changes and raise `JSONDecodeError` with #399 alone;
  - `ruff check .`, `make format-check`, `make workflow-check`, `make lint`, `make test-critical`.
- `windows.log`: native Windows 11, CPython 3.11.11 (no PostgreSQL, so those cases skip): the focused and related tests on the head and on a clean worktree of `8b220e14` with the new test files.
- `run-linux-testci.log`: the full `make test-ci` on the branch head.
