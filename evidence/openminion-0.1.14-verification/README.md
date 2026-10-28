# Release verification of openminion 0.1.14 (installed package)

This folder covers the published-package checks the maintainer asked for on openminion/openminion #390, #450 and #452. These issues are labelled `status:awaiting-release`; each checklist is the maintainer's comment after the fix was merged:
- #390: [comment 6441151990](https://github.com/openminion/openminion/pull/432#issuecomment-6441151990) on PR #432;
- #450: [comment 6461223433](https://github.com/openminion/openminion/issues/450#issuecomment-6461223433);
- #452: [comment 6462079883](https://github.com/openminion/openminion/issues/452#issuecomment-6462079883).

The fixes (#432, #451 and #453) were merged after 0.1.13 and are released in 0.1.14.

Everything below ran against `openminion==0.1.14` installed from PyPI into fresh virtual environments, not against a source checkout. The controls ran the same probes against `openminion==0.1.13` from PyPI in separate environments.

## Package provenance

| | |
|---|---|
| Install (Windows) | `uv venv --python 3.12 venv` and `uv pip install --python venv/Scripts/python.exe --index-url https://pypi.org/simple "openminion==0.1.14"` into a new venv; `openminion==0.1.13` the same way in a second venv for the controls (and `0.1.12` in a third, for the #452 rollback boundary only) |
| Install (Linux) | [`probes/linux_setup.sh`](probes/linux_setup.sh): `uv venv --python 3.12` and `uv pip install --index-url https://pypi.org/simple "openminion[postgres]==0.1.14"` (plus `openminion[postgres]==0.1.13` in a second venv for the controls) |
| Wheel | `openminion-0.1.14-py3-none-any.whl`, uploaded to PyPI 2026-10-28T08:42:35Z |
| sha256 (PyPI JSON) | `efd06138b2f10da019954539f5e33749aea4cd81727436d659efe0821345f3c4` |
| sha256 (downloaded wheel) | the same (match, on both machines) |
| Installed files | All 2744 hashed RECORD entries of the wheel are in the installed RECORD and match the installed files' sha256: 0 mismatched, 0 missing (Windows and Linux) |
| Imported module | Every probe prints `openminion.__file__` (under `<VENV>`'s `site-packages`) and `__version__` = `0.1.14`. Probes run from empty directories outside any checkout, with `python -I` |
| Windows | Native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4 |
| Linux | Ubuntu 24.04.5 LTS GitHub-hosted runner ([AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner), preset `none`: the checkout is the runner repo itself, not openminion), CPython 3.12.15, SQLite 3.45.1; PostgreSQL 16.15 (`postgres:16`), SQLAlchemy 2.1.6 and psycopg 3.3.6 for #390 |
| 0.1.13 (controls) | `openminion==0.1.13` from PyPI in a separate venv: wheel sha256 `266033a0a438b2fccc64b5ffec7a7929789108a4962347d6d57dfeb1cfd55897` matches PyPI, and all 2744 RECORD entries match |

Details are in [`probes/provenance.py`](probes/provenance.py) and [`windows/provenance/`](windows/provenance). Every runner run repeats the check in its own `provenance-linux.json`, and its `probe-sha256.txt` records the probe files as run.

## Release source contains each fix

Checked first with [`probes/release_source.py`](probes/release_source.py) (output [`release-source/release_source.json`](release-source/release_source.json)) and the GitHub API ([`release-source/github-api.txt`](release-source/github-api.txt)):

- The GitHub release `v0.1.14` was published 2026-10-28T08:46:30Z. The annotated tag `a029066d` points to commit `6f2c8f45` ("Merge pull request #459 from openminion/dev").
- For each of the 3 fixes, both the PR's merge commit and its reviewed head commit are ancestors of `6f2c8f45`: `git merge-base --is-ancestor`, and `behind_by: 0` from the GitHub compare API.
- The published archives equal the tag source. Every one of the 2738 `openminion/` files in the PyPI wheel is byte-identical to `src/openminion/` at the tag. Every one of the 2743 tag-tracked files in the PyPI sdist (`openminion-0.1.14.tar.gz`, sha256 `8bd76e00…1f5a`) is byte-identical to the tag; the other 8 sdist files are build metadata.
- Every source file #432 and #451 touched is in the wheel and the sdist and equals the tag. #453 touched only `CHANGELOG.md` and `docs/`, which are not in the archives; at the tag, `docs/runtime-database-upgrades.md` and `docs/README.md` equal the #453 head. The only file changed after the PRs' heads is the CHANGELOG.

| Issue | PR | Merge commit | Reviewed head |
|---|---|---|---|
| #390 | #432 | `68d623d1` | `e9203bdd` |
| #450 | #451 | `6273776b` | `88180dca` |
| #452 | #453 | `65ad947b` | `2d8fd446` |

## Results

Every check below ran on the published 0.1.14, and nothing failed on it.

| Issue | Maintainer's checklist (published package) | Result | 0.1.13 control | Evidence |
|---|---|---|---|---|
| #390 | A failed runtime memory assembly build after artifact/store/audit/service acquisition closes each owned resource exactly once; borrowed scheduler/vector resources stay usable; the original exception survives a failing cleanup; immediate SQLite file operations on native Windows; PostgreSQL disposes the owned engine; successful-assembly and external-backend controls | **PASS**: 82/82 on Windows, 115/115 on Linux (33 PostgreSQL checks) | 56/82 and 58/115 fail | [`windows/390`](windows/390), [`linux/390`](linux/390) (run [40665681581](https://github.com/AliceMessi/linux-runner/actions/runs/40665681581)) |
| #450 | Real subprocesses of `python -m openminion.modules.storage`: a successful plan exits 0; `pool-stats` without a URL and `a2a-archive-old` with zero days exit 2 with the existing JSON; an unknown namespace exits 1 with its exception; output and status match the existing CLI entry point; failures nonzero | **PASS**: 21/21 on Windows, 22/22 on Linux | 9/19 and 9/20 fail | [`windows/450`](windows/450), [`linux/450`](linux/450) (run [40665215948](https://github.com/AliceMessi/linux-runner/actions/runs/40665215948)) |
| #452 | The corrected guidance is published; its backup and rollback procedure from a clean install: inventory, accepted and refused files, every file backed up; checksums and integrity before anything moves; hot DELETE journals, stale WAL/SHM and companions among other files; working state matches the backup and a new turn continues; typed-save versus turn-start reset and the older-package rollback boundary | **PASS**: 71/71 on Windows, 62/62 on Linux | the 0.1.13 guidance restores only the runtime database and leaves the companions next to their databases | [`windows/452`](windows/452), [`linux/452`](linux/452) (run [40668632499](https://github.com/AliceMessi/linux-runner/actions/runs/40668632499)) |

The comment posted on each issue lists every checklist item with what ran and what was observed. The `.json` outputs hold every check with its observed values; the `.log` files hold the console output.

## Layout

- `probes/`: the shared provenance, setup and release-source scripts, and per issue `probe_<n>*.py`, `steps_<n>.sh` (runner steps) and `run_<n>_windows.sh`.
- `windows/<n>/`: Windows outputs and `probe-sha256.txt`; `control-0.1.13/` (or `older-0.1.13/` and `older-0.1.12/` for #452) for the older packages.
- `linux/<n>/`: the runner's outputs, `run-id.txt` and the run log; the 0.1.13 control files end in `_0113`.
- `release-source/`: `release_source.json` and `.log`, `prs.json` (each PR's merge commit, head and files from the GitHub API) and `github-api.txt`.

Local paths are replaced by `<WORK>`, `<VENV>` and similar placeholders. Files are stored byte-for-byte (`.gitattributes`: `* -text`).
