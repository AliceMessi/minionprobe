# OpenMinion: storage tests leave their own SQLite connections open (native Windows)

PR #385 disclosed that, on native Windows, `tests/test_storage_migrations.py::StorageMigrationTests::test_migrate_database_creates_expected_schema` and `::test_migrate_database_is_idempotent_on_rerun` fail in `TemporaryDirectory` cleanup, on `dev` too. The maintainer asked for a separate report in the #385 merge comment. This folder is the evidence for it, on `dev` @ `c9578ab6` (the #385 merge).

The tests read the database through `with sqlite3.connect(...) as conn:`. That context manager commits or rolls back but does not close the connection ([Python docs](https://docs.python.org/3/library/sqlite3.html#sqlite3-connection-context-manager)). When the temporary directory is removed, `conn` is still bound in the test, so Windows refuses to delete the file. The same thing happens in nearby storage tests, where a restore replaces a database that a test connection still holds.

Files:
- `probe_test_cleanup.py`: standard library only. It reproduces the pattern outside OpenMinion: the cleanup with `conn` still bound, after `del conn` with the cyclic garbage collector disabled (the connection's statement cache, a `functools._lru_cache_wrapper`, still refers to it), after `gc.collect()`, with `contextlib.closing(...)`, and the restore shape (`os.replace` onto the database).
- `trackconn.py`: a pytest plugin (`-p trackconn`). When `os.unlink` or `Path.replace` raises `PermissionError`, it prints the sqlite3 connections still open on that file and where they were opened.
- `closing-experiment.diff`: a mechanical experiment, not the proposed patch. Every bare `with sqlite3.connect(...) as x:` in five test files becomes `with closing(sqlite3.connect(...)) as x, x:`, and `test_runtime_bootstraps_storage_schema` also closes its `APIRuntime`.

Raw outputs (probe sha256 `80df0ebae2440a5afe799f7fba039942d346e690c52cc61dcd908d0d6ebbdfed`, diff sha256 `109151d07407a3bab64290e3f9c22d80247af1f3fcb81805ab07816bf8d7b0aa`, plugin sha256 `1419b19273dfc08caaa99a57764ac208b0056ced28bda9f5f48209c2d9a3789f`):
- `run-windows-c9578ab6.log`: native Windows 11, CPython 3.11.11, SQLite 3.47.1, a clean worktree of `c9578ab6`.
  - The probe, 5 identical runs.
  - The two tests, 5 runs: both fail every time with `PermissionError [WinError 32]` on `state/openminion.db`.
  - One full traceback.
  - The six test targets: 14 failed.
  - The same with the experiment applied: the two tests pass 5/5, and 2 of the 14 still fail. Those two are `storagectl restore` tests, where the open connection is storagectl's own record store, not a test connection (see the holders log); they are not part of this report.
- `holders-windows-c9578ab6.log`: the same six targets with `trackconn.py`, naming the connection that holds the file at each failure.
- `run-linux-c9578ab6.log`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17, SQLite 3.45.1, fresh checkout of `c9578ab6` (AliceMessi/linux-runner run 40185573269). The probe shows the same open connections, but the cleanup and the replace succeed. Everything passes, with and without the experiment.

Paths in the Windows logs are shortened to `<checkout>`, `<python>` and `<tmp>`, and the localized Windows error text is trimmed.

Reproduce from an OpenMinion source checkout: `PYTHONPATH="src;." python -m pytest -q -rf "tests/test_storage_migrations.py::StorageMigrationTests"` on Windows. Run `python probe_test_cleanup.py` from anywhere.
