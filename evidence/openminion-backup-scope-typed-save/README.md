# openminion: backup scope of a SQLite runtime home, and the working-state typed-save boundary

When the maintainer closed openminion/openminion #419, he asked for a separate documentation report:
- state exactly which files `openminion storage backup` supports;
- explain how to back up the rest of the owned data with the writers stopped;
- describe the typed-save boundary accurately, since the first turn-start reset can keep the old working-state JSON shape before the typed `save_state` adds `turn_evidence` and `turn_evidence_trace_id`.

This folder holds the evidence for that report: the issue draft and a documentation-only PR (branch `docs/backup-scope-typed-save`, commit `c490e2a9`, on `AliceMessi/openminion`).

Everything ran against installed packages in fresh `uv` venvs, from an empty directory, with `python -I -X utf8`. No checkout was on `sys.path`, and no repo test fixtures were used.

## Builds and machines

| Build | Windows | Linux |
|---|---|---|
| `openminion==0.1.13` from PyPI | `windows/0.1.13/` | `linux/*_0.1.13.*` |
| `dev` @ `68d623d10317a7cea13832f8dbd51fc98a871aa5` | `windows/dev-68d623d1/` | `linux/*_dev-68d623d1.*` |
| `openminion==0.1.12` from PyPI (only seeds the old working state) | `windows/pip-show-0.1.12.txt` | `linux/pip-show-venv0112.txt` |

- **Windows:** native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4, `sqlite3` shell 3.45.3 ([`windows/os.txt`](windows/os.txt)).
  - `dev` was installed from a `git archive` of the commit with `uv pip install <dir>`.
  - All 2736 `.py` files under `src/openminion` equal the commit's blobs, apart from CRLF line ends.
  - Every file the report cites is unchanged between `v0.1.13` and `68d623d1` ([`windows/dev-68d623d1/source.txt`](windows/dev-68d623d1/source.txt)).
- **Linux:** a GitHub-hosted Ubuntu 24.04.5 runner, CPython 3.12.15, SQLite 3.45.1, `sqlite3` shell 3.45.1 ([`linux/os.txt`](linux/os.txt)).
  - The run is [40503648435](https://github.com/AliceMessi/linux-runner/actions/runs/40503648435), preset `none`, so there was no openminion checkout.
  - `dev` was installed as `openminion @ git+https://github.com/openminion/openminion@68d623d1...` ([`probes/linux_setup.sh`](probes/linux_setup.sh)).
- `probe-sha256.txt` in `windows/` and `linux/` records the probe files as run. They are byte-identical to `probes/`.

## Results

| Probe | 0.1.13 Windows | dev Windows | 0.1.13 Linux | dev Linux |
|---|---|---|---|---|
| `probe_backup_scope.py` | 12/12 | 12/12 | 12/12 | 12/12 |
| `probe_typed_save_boundary.py` | 8/8 | 8/8 | 8/8 | 8/8 |

The observed values are the same in all four runs.

### `probe_backup_scope.py`

The home has one offline `echo` agent and the default layout: data root `<home>/.openminion`, runtime database `state/openminion.db`. Turns go through `APIRuntime.run_turn` with a session id and a conversation id, each turn in its own process.

1. After turn 1, the data root holds 17 SQLite files, found by file header with any extension. No `-wal`, `-shm` or `-journal` is left after the process exits.
2. `openminion storage backup --sqlite <file> --output <backup> --json` accepts the 7 files with `application_id` 0:
   - `state/openminion.db`, `state/brain/sessions.db`, `memory/memory.audit.db`;
   - `ops/evidence.db`, `ops/jobs.db`, `ops/plans.db`;
   - `task/task.db`.

   It refuses the 10 files that carry a module id, with exit 1 and `{"ok": false, "error": "application_id mismatch for module 'storage': expected 1330446336, found <id>"}`:
   - `artifact/index.db`, `authored_tools/store.sqlite`, `identity/identity.db`;
   - `memory/memory.db`, `state/brain/memory.db`;
   - `policy/policy.db`, `retrieve/retrieve.db`, `skill/skills.db`;
   - `state/brain/brain.db`, `telemetry/telemetry.db`.

   Each accepted backup equals its database and leaves a second copy in `<file>.bak/`.
3. For the 10 refused files, the documented Python one-liner (`sqlite3.Connection.backup`) and `sqlite3 <file> ".backup '<backup>'"` each give a backup equal to the database (`iterdump()` and `application_id`) that passes `integrity_check`.
4. Turn 2 changes 8 files after the backup, 5 of them refused ones.
5. Rollback with every process stopped: delete `-wal`/`-shm` if present (none were), then `openminion storage restore ... --yes --json` for the 7 accepted files and a file copy for the 10 others. All 17 files equal their backups.
6. The runtime reads the restored home:
   - `openminion sessions show` reports turn 1's count (2; it was 4 after turn 2);
   - the working state is turn 1's (versions 1-3);
   - turn 3 completes and continues from the restored state (its first row is version 4);
   - every file passes `integrity_check` afterwards.
7. On a scratch copy of `artifact/index.db`, a writer that exits without closing leaves a 12392-byte `-wal`. Copying the backup over the database with that `-wal` kept does not give the backup, because SQLite applies the old `-wal`. Deleting `-wal` and `-shm` first gives the backup.

### `probe_typed_save_boundary.py`

The published 0.1.12 writes rows 1-3 of a brain session `s-typed::conv:c1` and a `brainctl run` local session. The package under test then runs one turn in `c1`. A read-only recorder wraps `SessctlAdapter.put_working_state`: it records the caller frames, the payload keys and the version, then calls the original.

- **Row 4:** written by `services/brain/post_execution/mixin.py:170 _prepare_turn` -> `reset.py:782 _reset_state_for_new_input` -> `reset.py:73 _write_working_state_inline`. It has 0.1.12's key set and neither field. The package under test reads it with both fields `None`.
- **Rows 5-7:** written by `modules/brain/state.py:297 save_state`, reached from `state.py:175 load_or_init_state`, `state.py:574 respond` and `runner/coordinator.py:796`. All have both fields.
- **Controls:**
  - the next turn's reset row (8) has both fields;
  - a new conversation has no reset write, and its first row comes from `save_state` (`state.py:208`) with both fields;
  - `brainctl step` on the 0.1.12 local session saves `working_state.json` with both fields (`null`).

## Files

- `probes/probe_backup_scope.py`, `probes/probe_typed_save_boundary.py`: the probes. `probes/_common.py` is an unchanged copy of the shared helper from `openminion-0.1.13-verification`.
- `probes/run_windows.sh`: the Windows runs (variables `W` for the work root with the venvs and `E` for this folder).
- `probes/linux_setup.sh` and `probes/steps_linux.sh`: the runner setup and steps, sent with `runner_bundle.py --steps steps_linux.sh --no-postgres`.
- `windows/<build>/` and `linux/`: the `.log` and `.json` of each probe, plus `pip list` / `pip show`.

Paths are sanitized: `<VENV>` is the venv under test, `<VENV0112>` the 0.1.12 venv, `<VENVDEV>` the Linux dev venv, `<WORK>` the local work root, `<RUNNER_TEMP>` the runner's temp directory and `<USER_HOME>` the user's home.

## Not covered

- PostgreSQL runtime storage.
- Module databases that an echo turn does not create.
- The Windows `LocalSessionStore` `::conv:` id observation from #419, which is a separate report.

## Round 2

The maintainer's review of #453 found that the round 1 rollback steps left a hot rollback journal next to a restored file, and that the generic copy of the other files could put companions back. The corrected backup and rollback steps, and their validation, are in [`round2/`](round2/README.md).

## Round 3

The second review accepted the corrected steps. Check B of round 2's `probe_rollback_companions.py` was not portable: it asserted that the backup methods leave no `-wal`/`-shm` next to the database, while the macOS `sqlite3` shell leaves zero-byte ones. The probe now asserts the backup content, integrity, return code and the other-files exclusion instead, and records what each method leaves. See [`round3/`](round3/README.md).
