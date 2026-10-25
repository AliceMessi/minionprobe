# OpenMinion: `storagectl restore` replaces a database that its own record store holds open

Found while fixing #408. Two `storagectl restore` tests still failed on native Windows with the tests' own connections closed, and the open connection on the restore target belonged to storagectl. The maintainer asked for a separate production reproduction outside the tests (comment on #408). This folder is that reproduction, on `dev` @ `8b220e14`.

`storagectl` builds `StorageEngine.from_paths(..., sqlite_path=...)` before it dispatches the command (`modules/storage/cli.py:102-107`). The engine's SQLite record store connects to the `--sqlite` file in WAL mode (`modules/storage/record_store.py:323`). The `restore` branch (`cli.py:238-254`) then replaces that file with the snapshot (`MigrationRunner.restore` -> `restore_snapshot`, `migrations/backup.py:95-105`) and runs `runner.detect()`. The engine is closed only in `main`'s `finally` (`cli.py:118`).

Files:
- `probe_storagectl_restore.py` (sha256 `ee295c7a48abd09409d57fdbced12afcc60933121fd7bb97706fff4c2a10a7ee`): runs `storagectl migrate`, `backup` and `restore` as separate processes (`python -m openminion.modules.storage`), with OPENMINION_HOME and OPENMINION_DATA_ROOT in a temporary directory. No test code. It compares the restore target with the snapshot (a marker row, the table set and the file bytes) for:
  - A. `storagectl restore` onto the live database;
  - B. `storagectl restore` onto a new path;
  - C and D. controls: `MigrationRunner.restore`, the call that storagectl's restore branch makes, without the StorageEngine, onto the live database and onto a new path;
  - E. a trace of A and B in one process: the connections open on the target at the replace and where they were opened, the size of the `-wal` file next to the target, and whether the target equals the snapshot right after the replace, after `MigrationRunner.detect` and after `cli.main` returns.
- `probe-windows-8b220e14.log`: native Windows 11, CPython 3.11.11, SQLite 3.47.1, a clean worktree of `8b220e14`, 3 identical runs.
- `probe-linux-8b220e14.log`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17, SQLite 3.45.1, AliceMessi/linux-runner run 40190204361. The checkout is the #408 branch, whose `src/` is `8b220e14`'s (that branch changes only tests and the changelog).
- `probe-linux-177ec8f5-3runs.log`: the same probe on a fresh checkout of `dev` @ `177ec8f5`, 3 identical runs (run 40189767839). `8b220e14` only adds #409 (security tools); the output is identical.

Results:
- Windows: A and B exit 1 with `PermissionError: [WinError 5]` at the replace, and leave `<target>.restore.tmp` behind. After A the live database is unchanged. After B the new path holds the empty database that the record store created. C and D restore the snapshot. The only connection open on the target at the replace is the record store, opened from `cli.py:102 main` -> `StorageEngine.from_paths`.
- Linux: A restores the snapshot. B exits 0 and prints `"ok": true`, but the target is not the snapshot: 22 of the snapshot's 25 tables are missing, including the probe's marker table. The trace shows the target equal to the snapshot right after the replace, and different after `MigrationRunner.detect`. At the replace, the record store's connection is still open on the path and the `-wal` file next to it holds 103,032 bytes, the schema the record store wrote into the database it had just created. In A that `-wal` file is empty, since the record store's tables were already there from the earlier `storagectl backup`. C and D restore the snapshot.

Paths in the logs are shortened to `<tmp>`, and the localized Windows error text is trimmed.

Reproduce from an OpenMinion source checkout: `PYTHONPATH="src;." python probe_storagectl_restore.py` on Windows, `PYTHONPATH=src:. python probe_storagectl_restore.py` on Linux.
