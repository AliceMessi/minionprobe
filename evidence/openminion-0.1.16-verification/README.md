# Release verification of openminion 0.1.16 (installed package)

This folder covers the published-package checks the maintainer asked for on these openminion/openminion issues. Each issue is labelled `status:awaiting-release`, and each checklist is the maintainer's comment after the fix was merged:
- #447: [comment 6487917902](https://github.com/openminion/openminion/issues/447#issuecomment-6487917902);
- #448: [comment 6488733370](https://github.com/openminion/openminion/issues/448#issuecomment-6488733370);
- #493: [comment 6490537757](https://github.com/openminion/openminion/issues/493#issuecomment-6490537757);
- #494: [comment 6496868768](https://github.com/openminion/openminion/issues/494#issuecomment-6496868768);
- #495: [comment 6494402314](https://github.com/openminion/openminion/issues/495#issuecomment-6494402314);
- #496: [comment 6490596301](https://github.com/openminion/openminion/issues/496#issuecomment-6490596301).

The fixes are #470 (#447), #471 (#448), #519 (#493), #521 (#494), #520 (#495) and #525 (#496). The 0.1.16 release notes ("Current package line - 2026-10-29") list exactly these six.

Everything below ran against `openminion==0.1.16` installed from PyPI into fresh virtual environments, not against a source checkout. The controls ran the same probes against `openminion==0.1.15` from PyPI in separate environments.

## Package provenance

| | |
|---|---|
| Install (Windows) | `uv venv --python 3.12` and `uv pip install --index-url https://pypi.org/simple "openminion==0.1.16"` into a new venv; `openminion==0.1.15` the same way in a second venv for the controls |
| Install (Linux) | [`probes/linux_setup.sh`](probes/linux_setup.sh): `uv pip install --index-url https://pypi.org/simple "openminion[postgres]==0.1.16"` (plus `openminion[postgres]==0.1.15` in a second venv for the controls) on a GitHub-hosted runner with no openminion checkout. The package spec is one variable (`OM_SPEC`), so the same scripts can run a release candidate from git; every run here used the PyPI default (each run's `om-spec.txt`) |
| Wheel | `openminion-0.1.16-py3-none-any.whl`, uploaded to PyPI 2026-10-30T04:38:48Z; sdist `openminion-0.1.16.tar.gz` 2026-10-30T04:38:50Z; GitHub release `v0.1.16` published 2026-10-30T04:42:05Z |
| sha256 (PyPI JSON) | `d320ede956aa85f13949d157494ad822ca4ac5f0744f763099d589e9f1b8e2ca` |
| sha256 (downloaded wheel) | the same, on Windows and on every Linux run |
| Installed files | All 2757 hashed RECORD entries of the wheel match the installed files: 0 mismatched, 0 missing, on Windows ([`windows/provenance-windows.json`](windows/provenance-windows.json)) and on Linux (each run's `provenance-linux.json`) |
| Imported module | Every probe prints `openminion.__file__` (under `<VENV>`'s `site-packages`) and `__version__` = `0.1.16`. Probes run from empty directories outside any checkout, with `python -I` |
| Windows | Native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4 |
| Linux | Ubuntu 24.04.5 GitHub-hosted runner ([AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner)), CPython 3.12.15, SQLite 3.45.1; PostgreSQL 16.15 (`postgres:16`), SQLAlchemy 2.1.6 and psycopg 3.3.6 |
| 0.1.15 (controls) | `openminion==0.1.15` from PyPI in a separate venv: wheel sha256 `133e403668f24219b66ea1789902229d225e3570fae4c5ede3644746b1594e47`, matching PyPI on Windows ([`windows/provenance-windows-0.1.15.json`](windows/provenance-windows-0.1.15.json)) and on Linux (`provenance-linux-0.1.15.json`) |

## Release source contains each fix

Checked first with [`probes/release_source.py`](probes/release_source.py); the output is [`release-source/release_source.json`](release-source/release_source.json), and the PR data is in [`release-source/prs.json`](release-source/prs.json):
- The GitHub release `v0.1.16` was published 2026-10-30T04:42Z. The annotated tag `18837f8e` points to commit `6fcb5852`, which is also `main`.
- For each of the 6 fixes, both the PR's merge commit and its reviewed head commit are ancestors of `6fcb5852` (`git merge-base --is-ancestor`).
- The published archives equal the tag source:
  - every one of the 2751 `openminion/` files in the PyPI wheel is byte-identical to `src/openminion/` at the tag;
  - every one of the 2756 tag-tracked files in the PyPI sdist (`openminion-0.1.16.tar.gz`, sha256 `1a9023c5…366d`) is byte-identical to the tag; the other 8 sdist files are build metadata.
- Every source file a fix PR changed is in the wheel and the sdist and equals the tag, except the two package READMEs (`api/README.md`, `memory/README.md`), which are not packaged. Where a fix's file changed again after its reviewed head, the later change is another of these six fixes: #525 after #471 (`coordinator.py`, `post_execution/mixin.py`, `memory/README.md` and the #448 tests), #521 after #519 (`storage/migrations/alembic.py`), and #520's test added to `test_runtime_storage_postgres.py` after #470.

| Issue | PR | Merge commit | Reviewed head |
|---|---|---|---|
| #447 | #470 | `ce3b6771` | `81c795f3` |
| #448 | #471 | `2df62d9b` | `6064d6d7` |
| #493 | #519 | `455838a1` | `fc4e5007` |
| #494 | #521 | `48332187` | `77f9013b` |
| #495 | #520 | `aec2df9e` | `28ab7dbd` |
| #496 | #525 | `c7a034b8` | `4b4269d8` |

## Results

| Issue | Result on 0.1.16 | 0.1.15 control | Evidence |
|---|---|---|---|
| #447 | **PASS**: 243/243 on Linux (SQLite and PostgreSQL), 135/135 on Windows; the report's own probes, unchanged, 34/34 and 21/21. Every injected step before construction and after the ownership handoff (including the runtime manager and bind), the real `MigrationError` and the unsupported-provider `ConfigError` leave 0 open files, 0 start threads and 0 PostgreSQL connections, with automatic gc off and 0 `gc.collect` calls; on Windows every owned file is renamed and removed right away. The close-failure, aliased-store and borrowed-telemetry cases pass; a successful start/turn/close and `StorageEngine.close()` are unchanged | 130/239 on Linux, 74/133 on Windows. A failed start closes nothing: up to 9 SQLite databases open, Windows files locked (`WinError 32`), 1 PostgreSQL connection, the emitter and sweeper threads alive | [`windows/447`](windows/447), [`linux/447`](linux/447) |
| #448 | **PASS**: 39/39 Windows SQLite, 39/39 Linux SQLite, 41/41 Linux PostgreSQL (runtime storage and memory store). `require_approval` and `deny` turns make no extraction request and no capture write, and borrow or ask no approval; the intent stays pending with `CAPTURE_RECOVERY_UNAUTHORIZED` and is recovered once, with no duplicates on replay; one runner follows allow/deny per turn with the turn's own context; `agent-check` and `doctor --check-turn` make 0 extraction requests; the #468 controls hold | 19/39, 19/39, 21/41. Denied turns extract and write the candidate, receipt and result; diagnostic turns make an extraction request; there is no per-turn decision | [`windows/448`](windows/448), [`linux/448`](linux/448) |
| #493 | **PASS**: 30/30 Windows SQLite, 30/30 Linux SQLite, 48/48 PostgreSQL (the `public` schema of a new database and a named `om_compress` schema). `storage migrate --module compress` reaches `0001_baseline` with its tables, `om_meta` and `application_id`; the rerun is clean and keeps data; compress's own `run_migrations` works; discovery finds all 17 migrations packages and the other modules still migrate | 10/30, 10/30, 11/48. `Alembic configuration missing` and nothing written; compress not discovered (16 of 17); `run_migrations` raises | [`windows/493`](windows/493), [`linux/493`](linux/493) |
| #494 | **PASS**: 69/69 on PostgreSQL in module-owned schemas (heads, clean rerun, store round trips with SERIAL ids and exact `double precision` timestamps, store-created tables kept, 5 incompatible tables refused with nothing written and no ledger stamp, downgrade/re-upgrade, rollback on real and injected failures); 24/24 SQLite on Linux and on Windows (history and migrated files identical to 0.1.15; controlplane opens a baseline-created file without duplicating columns) | 14/69: every PostgreSQL migrate fails with `syntax error at or near "AUTOINCREMENT"`; 9/13 SQLite: the controlplane store fails with `duplicate column name: next_attempt_at` | [`windows/494`](windows/494), [`linux/494`](linux/494) |
| #495 | **PASS**: 42/42 on Windows SQLite, Linux SQLite and PostgreSQL. Both selection-versus-leave interleavings on independent connections are serialized by the session-row lock; no departed active agent in the stress runs or after reopening; selection, refusal, legacy-owner, join/activation and rollback behavior as specified. #393 is kept separate | 24/42 on each. Both interleavings store the departed agent; the stress stores it in 70/86, 49/86 and 17/30 of the rooms where both calls start together | [`windows/495`](windows/495), [`linux/495`](linux/495) |
| #496 | **PASS**: 24/24 Windows SQLite, 24/24 Linux SQLite, 17/17 PostgreSQL memory store. Denied and confirmation-required turns write 0 decision records and 0 `tool_outcome` candidates, no refused arguments in the memory store, and no new approval request; the allowed, no-identity and no-policy controls equal 0.1.15; one runner follows each turn's answer across allow/deny/allow, consolidation and diagnostic turns; the unauthorized capture stays pending and is recovered once. #524 is not claimed | 9/22, 9/22, 4/15. Denied turns write 4 decision records and the candidate with the refused arguments; no turn asks `memory.write` | [`windows/496`](windows/496), [`linux/496`](linux/496) |

On the controls, the checks that still pass are the behavior each fix had to keep (success paths, errors reaching the caller, unchanged CLI output, the controls named in each checklist); the checks that fail are the reported bugs.

Final runner runs (each one runs 0.1.16 and the 0.1.15 control): #447 [41006125743](https://github.com/AliceMessi/linux-runner/actions/runs/41006125743), #448 [41006117435](https://github.com/AliceMessi/linux-runner/actions/runs/41006117435), #493 [41005523312](https://github.com/AliceMessi/linux-runner/actions/runs/41005523312), #494 [41005925206](https://github.com/AliceMessi/linux-runner/actions/runs/41005925206), #495 [41005841032](https://github.com/AliceMessi/linux-runner/actions/runs/41005841032), #496 [41005766508](https://github.com/AliceMessi/linux-runner/actions/runs/41005766508). Each `linux/<n>/` folder equals a fresh download of its run's artifact (after the path replacement below), and the probe hashes the runner and Windows recorded (`probe-sha256.txt`) equal the files in `probes/`.

The comment posted on each issue lists every checklist item with what ran and what was observed. The `.json` outputs hold every check with its observed values (each check name starts with the checklist item it covers); the `.log` files hold the console output.

## Layout

- `probes/`:
  - the shared provenance, setup and release-source scripts (`_common.py` is byte-identical to the 0.1.15 round's);
  - for each issue: `probe_<n>*.py`, `steps_<n>.sh` (runner steps) and `run_<n>_windows.sh`;
  - `probe_420_lib.py` is a byte copy of the earlier scripted-provider harness (SHA-256 `6a00f9f7...`) and keeps its CRLF line endings;
  - `probe_447_report_retention.py` and `probe_447_report_repro.py` are byte copies of the #447 report's `probe_failed_start_retention.py` (`6d556cd1...`) and `repro_failed_start.py` (`da974dfc...`); `probe_447_report_checks.py` evaluates their outputs.
- `windows/<n>/`: the Windows outputs and `probe-sha256.txt`, with `control-0.1.15/` for the control; `windows/provenance-*.json`: the Windows package provenance.
- `linux/<n>/`: the runner's outputs, `run-id.txt` and the run log. The 0.1.15 control files end in `_0115`.
- `release-source/`: `release_source.json` and `.log`, and `prs.json` (each PR's merge commit, head and files, from the GitHub API).
- [`SHA256SUMS.txt`](SHA256SUMS.txt): every file in this folder with its sha256.

Local paths are replaced by placeholders: `<WORK>` (the local work folder), `<VENV>` (the 0.1.16 venv), `<VENV0115>` (the 0.1.15 venv; in the #494 SQLite outputs the 0.1.16 run's reference to it reads `<VENV>0115`), `<RUNNER_TEMP>`, `<CASE_WORK>` (a probe case's work folder) and `<EVIDENCE>` (this folder, in the #447 Windows outputs). Files are stored byte-for-byte (`.gitattributes`: `* -text`), with LF line endings except `probes/probe_420_lib.py`.
