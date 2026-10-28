# Round 3: a portable check B in `probe_rollback_companions.py`

The maintainer's second review of openminion/openminion PR #453 accepted the corrected rollback steps. He ran the documented procedure on installed 0.1.13 on macOS.

One check of our external probe was not portable. Round 2's check B asserted that no `-journal`, `-wal` or `-shm` is left next to the database after the backup is taken. With the macOS `sqlite3` shell 3.51.0, `.backup` leaves a zero-byte `-wal` and `-shm`, so the probe reported 17/18 there. The backup content, integrity and return code passed in all six controls. The guide already leaves the companions out of the other-files copy and does not require them to disappear, so this was a wrong assertion in the probe, not a backup failure.

Only this probe changed. The docs it follows are the PR's, now at head `2d8fd446` (a merge of `dev` @ `4083b663`; the docs are unchanged from `9d109ec9`).

## What changed in the probe

The full change is in [`probe_rollback_companions.round2-to-round3.diff`](probe_rollback_companions.round2-to-round3.diff).

- **Check B no longer asserts that a backup method removes the companions.** It now asserts, for `openminion storage backup`, the Python one-liner and `sqlite3 <file> ".backup"`, with a hot `-journal` or a committed `-wal` next to the database:
  - the command exits 0;
  - the backup holds the committed content (`committed` / `committed-in-wal`) and passes `integrity_check`;
  - the other-files copy (backup step 3) holds only `notes.json`, so every companion still next to the database is left out, whatever its size.
- **The companions each method leaves behind are recorded, not asserted**, in `observed.B_companions_left_by_backup_method`. That is where the platform difference shows.
- **A new check B makes the exclusion run on every platform.** It puts a zero-byte `-wal` and `-shm` next to the database between the backup and the other-files copy, which is what the macOS shell leaves. The backup holds `committed`, and the copy leaves both companions out. It runs for the accepted file (`openminion storage backup`) and the refused one (the Python one-liner).
- `take_backup` gained an optional hook, `before_copy`, which runs between the database backup and the other-files copy. Only the new check uses it.
- Checks J, W, O and V are unchanged. The probe now has 19 checks instead of 18.

## Builds and machines

| Build | Windows | Linux |
|---|---|---|
| `openminion==0.1.13` from PyPI | `windows/0.1.13/` | `linux/*_0.1.13.*` |
| PR head `2d8fd446f699c1c96345607fbcdb475c9ec85942` (version string 0.1.14), installed from `git+https://github.com/AliceMessi/openminion@<sha>` | `windows/head-2d8fd446/` (with `direct_url.json`) | `linux/*_head-2d8fd446.*` |

- **Windows:** native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4, `sqlite3` shell 3.45.3 ([`windows/os.txt`](windows/os.txt)). Fresh `uv` venvs.
- **Linux:** a GitHub-hosted Ubuntu runner, run [40638234311](https://github.com/AliceMessi/linux-runner/actions/runs/40638234311), preset `none` (no openminion checkout). Ubuntu 24.04.5, CPython 3.12.15, SQLite 3.45.1, `sqlite3` shell 3.45.1 ([`linux/os.txt`](linux/os.txt)).
- The probe runs on the installed package with `python -I -B -X utf8`, from an empty directory, with no checkout on `sys.path` and no repo test fixtures.
- `probe-sha256.txt` in `windows/` and `linux/` records the files as run. They are byte-identical to `probes/`.

## Results

| Build | Windows | Linux |
|---|---|---|
| `openminion==0.1.13` | 19/19 | 19/19 |
| head `2d8fd446` | 19/19 | 19/19 |

Companions left next to the database by each backup method (`B_companions_left_by_backup_method`):

| Backup method | Windows (shell 3.45.3) | Linux (shell 3.45.1) |
|---|---|---|
| `openminion storage backup` (accepted file), hot `-journal` / committed `-wal` | none / none | none / none |
| Python one-liner (refused file), hot `-journal` / committed `-wal` | none / none | none / none |
| `sqlite3 <file> ".backup '<backup>'"`, hot `-journal` / committed `-wal` | none / none | none / none |

The same on both builds.

The maintainer's macOS observation (a zero-byte `-wal` and `-shm` after the shell's `.backup`, shell 3.51.0) is not reproduced on these machines. The new check B covers it by producing the same files on every platform.

## Files

- `probes/probe_rollback_companions.py`: the round 3 probe.
- `probes/_common.py`: unchanged from round 2.
- `probes/run_windows.sh`: the Windows runs (variables `W` for the work root with the venvs and `E` for this folder).
- `probes/linux_setup.sh` and `probes/steps_linux.sh`: the runner setup and steps, sent with `runner_bundle.py --steps steps_linux.sh --no-postgres`.
- `windows/<build>/` and `linux/`: the `.log` and `.json` of each run, plus `pip list` / `pip show`.

Paths are sanitized: `<VENV>` is the venv under test, `<WORK>` the local work root, `<RUNNER_TEMP>` the runner's temp directory, `<TMP>` the temp directory and `<USER_HOME>` the user's home.
