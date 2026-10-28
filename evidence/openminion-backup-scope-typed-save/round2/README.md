# Round 2: rollback steps that keep SQLite journal and WAL companions out of a restore

The maintainer's review of openminion/openminion PR #453 found a problem in the round 1 rollback steps. Those steps deleted `<file>-wal` and `<file>-shm`, then put each backup in place, but:
- they left a hot `<file>-journal` next to the restored file;
- the generic "other files" copy could put companions back after they were deleted.

He reproduced it with SQLite DELETE journaling. The first read of the restored file gave `after-backup` instead of the backup.

The correction (branch `docs/backup-scope-typed-save`, commit `0082100c`, merged with `dev` @ `988ac4ec` as `9d109ec9`) changes the steps in `docs/runtime-database-upgrades.md`.

**Backup:**
- The other files are copied without the `-journal`, `-wal` and `-shm` companions.
- A checksum is recorded for each backup.

**Rollback:**
1. Stop every process that uses the runtime home.
2. Check each backup against its checksum and `PRAGMA integrity_check`.
3. Move each database aside together with its companions, without deleting any of them.
4. Put the backups in place, then copy the other files back without companions.
5. Check that no companion is next to a restored file.

This folder validates the corrected steps exactly as written. It also shows where the round 1 steps fail.

Everything ran against installed packages in fresh `uv` venvs, from an empty directory, with `python -I -X utf8`. No checkout was on `sys.path`, and no repo test fixtures were used.

## Builds and machines

| Build | Windows | Linux |
|---|---|---|
| `openminion==0.1.13` from PyPI | `windows/0.1.13/` | `linux/*_0.1.13.*` |
| `dev` @ `988ac4ec54ae803f4ac937a6fc2fc1c04bb5f590` | `windows/dev-988ac4ec/` | `linux/*_dev-988ac4ec.*` |
| `openminion==0.1.12` from PyPI (only seeds the old working state) | `windows/pip-show-0.1.12.txt` | `linux/pip-show-venv0112.txt` |

- **Windows:** native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4, `sqlite3` shell 3.45.3 ([`windows/os.txt`](windows/os.txt)).
  - `dev` was installed from a `git archive` of the commit. Its 2736 `.py` files match the archive ([`windows/dev-988ac4ec/source.txt`](windows/dev-988ac4ec/source.txt)).
- **Linux:** a GitHub-hosted Ubuntu 24.04.5 runner, CPython 3.12.15, SQLite 3.45.1, `sqlite3` shell 3.45.1 ([`linux/os.txt`](linux/os.txt)).
  - The run is [40630635076](https://github.com/AliceMessi/linux-runner/actions/runs/40630635076), preset `none` (no openminion checkout).
  - `dev` was installed from `git+https://github.com/openminion/openminion@988ac4ec...` ([`probes/linux_setup.sh`](probes/linux_setup.sh)).
- `probe-sha256.txt` in `windows/` and `linux/` records the probe files as run. They are byte-identical to `probes/`.

## Results

| Probe | 0.1.13 Windows | dev Windows | 0.1.13 Linux | dev Linux |
|---|---|---|---|---|
| `probe_rollback_companions.py` (new) | 18/18 | 18/18 | 18/18 | 18/18 |
| `probe_backup_scope.py` (rollback steps updated) | 17/17 | 17/17 | 17/17 | 17/17 |
| `probe_typed_save_boundary.py` (unchanged from round 1) | 8/8 | 8/8 | 8/8 | 8/8 |

The observed values are the same in all four runs.

### `probe_rollback_companions.py`

Each case builds a small database `data/module.db` (table `marker`, value `backup`) and backs it up as documented. Then the database is changed and companions are left the way a writer that did not finish leaves them. Two identical copies are rolled back, one with the round 1 steps and one with the corrected steps.

Every case runs for:
- an **accepted** file (`application_id` 0), backed up with `openminion storage backup` and restored with `openminion storage restore`;
- a **refused** file (`application_id` of the artifact module), backed up with the documented Python one-liner and restored with a file copy.

| Case | Round 1 steps | Corrected steps |
|---|---|---|
| **J**: the maintainer's crashed DELETE-journal writer leaves a 9728-byte hot `-journal` after `after-backup` was committed | No `-wal`/`-shm` to delete, and the journal stays. The first read is `after-backup` | Backup checked (checksum and `integrity_check`, which leaves it unchanged). `module.db` and `module.db-journal` move aside, the backup goes in place, no companion is left, and the first read is `backup`. The set moved aside, opened together, rolls back to `after-backup` |
| **W**: a WAL writer commits `after-backup` and exits without closing; `-wal` (4152 bytes) and `-shm` stay, and the file alone still reads `backup` | `-wal`/`-shm` are deleted and the restored file reads `backup`, but the deleted `-wal` was the only copy of the `after-backup` commit. Keeping them (no deletion) reads `after-backup` | `module.db`, `-wal` and `-shm` move aside and the restored file reads `backup`. The set moved aside still reads `after-backup` |
| **O**: the copied other files hold a `-wal` and `-shm` with a newer commit | The current `-wal`/`-shm` are deleted, the old ones are copied back next to the restored file, and the first read is `after-backup` | The other files are copied back without companions, and the first read is `backup` |
| **B**: a hot `-journal`, or a `-wal` with a commit, is next to the database while the backup is taken | — | `openminion storage backup`, the Python one-liner and `sqlite3 <file> ".backup '<backup>'"` all open the database through SQLite. The backup holds the committed content (`committed` / `committed-in-wal`, `integrity_check` ok), and no companion is left next to the database to copy |
| **V**: the backup no longer matches its recorded SHA-256 (one byte changed) | — | Step 2 stops the rollback before anything is moved. The database and its hot journal are unchanged byte for byte |

### `probe_backup_scope.py`

This is round 1's runtime-home probe, with the backup and rollback steps as now documented.

The home has one offline `echo` agent and the default layout, and has 17 SQLite files after turn 1:
- 7 are accepted by `openminion storage backup`;
- 10 are refused and backed up with SQLite's backup API, from Python and from the `sqlite3` shell.

**Backup steps 3-4:** 9 other files are copied, all of them in `<file>.bak/` directories, with no companion present. A SHA-256 is recorded for each of the 17 backups.

**After the backup:**
- Turn 2 changes 8 files.
- Then writers that do not finish leave companions:
  - a commit, then a crashed writer's hot `-journal` on `ops/evidence.db` (accepted, DELETE journal);
  - `-wal` and `-shm` with a commit on `memory/memory.audit.db` (accepted, WAL);
  - `-wal` and `-shm` with a commit on `artifact/index.db` (refused, WAL).

**Round 1 steps, on a raw copy of the data root:**
- `openminion storage restore` puts the backup of `ops/evidence.db` in place next to the hot journal. Opening it then fails with `DatabaseError: database disk image is malformed`.
- For the two WAL files, deleting `-wal`/`-shm` gives the backups, but the commits made after the backup are lost.

**Corrected steps, on the home:**
- All 17 backups match their SHA-256, and `sqlite3 <backup> "PRAGMA integrity_check"` prints `ok`.
- Every SQLite file moves aside with its companions, keeping its path relative to the data root.
- `openminion storage restore ... --yes --json` restores the 7 accepted files (exit 0, `ok`), and a file copy restores the 10 refused ones.
- The other files are copied back, and no companion is next to a restored file.
- All 17 files equal their backups and pass `integrity_check`.
- The sets moved aside keep the current content:
  - `ops/evidence.db`, opened with its journal, rolls back to its last commit;
  - the two WAL files still hold their commits.

**The runtime reads the restored home**, as in round 1:
- `openminion sessions show` reports turn 1's count;
- the working state is turn 1's (versions 1-3);
- turn 3 continues from it (its first row is version 4), and every file passes `integrity_check`.

### `probe_typed_save_boundary.py`

Unchanged from round 1 (same file, see the parent folder's README). It passes 8/8 on both builds and both machines.

## Other checks on head `9d109ec9`

- `pytest -q tests/test_package_layout.py tests/test_package_metadata.py -k "not built_archives"` -> 35 passed, 1 deselected. This ran on native Windows with `python -X utf8` and `PYTHONPATH="src;."`.
- Every relative link and `#` anchor in `docs/runtime-database-upgrades.md` and `docs/README.md` resolves.
- `git diff --check upstream/dev` is clean. The commit message passes `scripts/commit_message.py`.
- The merge with `dev` @ `988ac4ec` keeps all three Unreleased CHANGELOG entries: #452/#419, #390 and #435.

## Files

- `probes/probe_rollback_companions.py`: the new probe.
- `probes/probe_backup_scope.py`: round 1's probe with the corrected backup and rollback steps. Step 8 of round 1, the `-wal` demo, is replaced by case W.
- `probes/probe_typed_save_boundary.py` and `probes/_common.py`: unchanged copies.
- `probes/run_windows.sh`: the Windows runs (variables `W` for the work root with the venvs and `E` for this folder).
- `probes/linux_setup.sh` and `probes/steps_linux.sh`: the runner setup and steps, sent with `runner_bundle.py --steps steps_linux.sh --no-postgres`.
- `windows/<build>/` and `linux/`: the `.log` and `.json` of each probe, plus `pip list` / `pip show`.

Paths are sanitized:
- `<VENV>` is the venv under test and `<VENV0112>` the 0.1.12 venv;
- `<WORK>` is the local work root and `<RUNNER_TEMP>` the runner's temp directory;
- `<TMP>` is the temp directory and `<USER_HOME>` the user's home.
