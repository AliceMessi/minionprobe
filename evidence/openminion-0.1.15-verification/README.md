# Release verification of openminion 0.1.15 (installed package)

This folder covers the published-package checks the maintainer asked for on these openminion/openminion issues. Each issue is labelled `status:awaiting-release`, and each checklist is the maintainer's comment after the fix was merged:
- #437: [comment 6466090946](https://github.com/openminion/openminion/issues/437#issuecomment-6466090946);
- #364: [comment 6441337657](https://github.com/openminion/openminion/issues/364#issuecomment-6441337657), the remaining room CLI check on PostgreSQL;
- #369: [comment 6417425007](https://github.com/openminion/openminion/issues/369#issuecomment-6417425007), the remaining full runtime/CLI path on PostgreSQL;
- #449: [comment 6475566061](https://github.com/openminion/openminion/issues/449#issuecomment-6475566061);
- #456: [comment 6470146754](https://github.com/openminion/openminion/issues/456#issuecomment-6470146754);
- #393: [comment 6485541844](https://github.com/openminion/openminion/issues/393#issuecomment-6485541844);
- #446, its reporting part only: [comment 6470206553](https://github.com/openminion/openminion/issues/446#issuecomment-6470206553).

The fixes are #458 (#437), #457 (#446 reporting), #468 (#449), #469 (#393) and the maintainer's #463 (#456). #364 and #369 were fixed in 0.1.12 by #385 and #406. Their remaining PostgreSQL entry points needed #458.

Everything below ran against `openminion==0.1.15` installed from PyPI into fresh virtual environments, not against a source checkout. The controls ran the same probes against `openminion==0.1.14` from PyPI in separate environments.

## Package provenance

| | |
|---|---|
| Install (Windows) | `uv venv --python 3.12` and `uv pip install --index-url https://pypi.org/simple "openminion==0.1.15"` into a new venv; `openminion==0.1.14` the same way in a second venv for the controls |
| Install (Linux) | [`probes/linux_setup.sh`](probes/linux_setup.sh): `uv pip install --index-url https://pypi.org/simple "openminion[postgres]==0.1.15"` (plus `openminion[postgres]==0.1.14` in a second venv for the controls) on a GitHub-hosted runner with no openminion checkout |
| Wheel | `openminion-0.1.15-py3-none-any.whl`, uploaded to PyPI 2026-10-29T03:52:14Z |
| sha256 (PyPI JSON) | `133e403668f24219b66ea1789902229d225e3570fae4c5ede3644746b1594e47` |
| sha256 (downloaded wheel) | the same |
| Installed files (Linux) | All 2757 hashed RECORD entries of the wheel match the installed files: 0 mismatched, 0 missing (each run's `provenance-linux.json`) |
| Imported module | Every probe prints `openminion.__file__` (under `<VENV>`'s `site-packages`) and `__version__` = `0.1.15`. Probes run from empty directories outside any checkout, with `python -I` |
| Windows | Native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4 |
| Linux | Ubuntu 24.04 GitHub-hosted runner ([AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner)), CPython 3.12.15, SQLite 3.45.1; PostgreSQL 16.15 (`postgres:16`), SQLAlchemy 2.1.6 and psycopg 3.3.6 |
| 0.1.14 (controls) | `openminion==0.1.14` from PyPI in a separate venv: wheel sha256 `efd06138b2f10da019954539f5e33749aea4cd81727436d659efe0821345f3c4` |

## Release source contains each fix

Checked first with [`probes/release_source.py`](probes/release_source.py); the output is [`release-source/release_source.json`](release-source/release_source.json), and the PR data is in [`release-source/prs.json`](release-source/prs.json):
- The GitHub release `v0.1.15` was published 2026-10-29T03:54Z. The annotated tag `18c6d500` points to commit `d631e8c6`.
- For each of the 5 fixes, both the PR's merge commit and its reviewed head commit are ancestors of `d631e8c6` (`git merge-base --is-ancestor`).
- The published archives equal the tag source:
  - every one of the 2751 `openminion/` files in the PyPI wheel is byte-identical to `src/openminion/` at the tag;
  - every one of the 2756 tag-tracked files in the PyPI sdist (`openminion-0.1.15.tar.gz`, sha256 `8f0027df…989a`) is byte-identical to the tag; the other 8 sdist files are build metadata.

| Issue | PR | Merge commit | Reviewed head |
|---|---|---|---|
| #437 | #458 | `eea9fd0f` | `66521353` |
| #446 (reporting) | #457 | `63ac9a17` | `29e19c3a` |
| #449 | #468 | `71b9f892` | `34ccf868` |
| #393 | #469 | `d5787c96` | `af5eb7b1` |
| #456 | #463 | `042e0a2c` | `a4f96ccb` |

## Results

| Issue | Result on 0.1.15 | 0.1.14 control | Evidence |
|---|---|---|---|
| #437 | **PASS**: 42/42 on Linux (13 SQLite controls, 29 PostgreSQL checks), 13/13 on Windows (SQLite controls). Startup through the storage section and the environment forms, `storage_connection` and the wrapper's `storage` None on PostgreSQL, one SDK turn and the real `openminion room` CLI persisting their rows in PostgreSQL with no `state.db`, 0 server connections after close or exit | Every PostgreSQL start raises `runtime record_store does not expose sqlite connection`; `room create` exits 1 | [`windows/437`](windows/437), [`linux/437`](linux/437) |
| #364 | **PASS** for the remaining item: 18/18 on PostgreSQL through the real room CLI and `openminion run`, 16/16 on Linux and Windows SQLite | On PostgreSQL every room command exits 1 (11 of 12 checks fail) | [`windows/364`](windows/364), [`linux/364`](linux/364) |
| #369 | **PASS** for the remaining part: 21/21 on PostgreSQL (the real `openminion acp` stdio server's `session/list`, Focus over a real runtime and the bare `openminion --dir` launch), 16/16 Linux SQLite, 6/6 Windows SQLite | `openminion acp` exits 2 and the Focus launch exits 1 on PostgreSQL | [`windows/369`](windows/369), [`linux/369`](linux/369) |
| #449 | **PASS**: 25/25 on Windows, 32/32 on Linux with PostgreSQL. Repeated recovery against an unsupported store: 0 source reads, extraction requests, bundle writes or result writes in 14 runs; one recovery after reopening with a capable store, also behind the audit wrapper | 102 extraction requests inside recovery, pending growing from 1 to 15 | [`windows/449`](windows/449), [`linux/449`](linux/449) |
| #456 | **PARTIAL**: every runtime item passes for new jobs (19/19) and for a job created with 0.1.14 and then upgraded (10/10), on Windows and Linux. The documented inspect/recreate procedure for old jobs is **NOT AVAILABLE**: 0.1.15 publishes no such documentation | Self-capture, the reserved payload accepted, a forged row running as a consolidation turn | [`windows/456`](windows/456), [`linux/456`](linux/456) |
| #393 | **PASS**: 49/49 on Windows SQLite, 49/49 on Linux SQLite, 49/49 on PostgreSQL; unpaused stress with 200 rooms per case: 0 violations. #495 (selection versus leave, PR #520) is not in 0.1.15 and is not claimed | Stale overwrite and departed active agent reproduce (27 or 28 of 49 checks fail) | [`windows/393`](windows/393), [`linux/393`](linux/393) |
| #446 (reporting) | **PASS**: 20/20 on Windows SQLite, Linux SQLite and PostgreSQL through the real `openminion storage migrate` console script. The actual revision error appears in text and JSON, there is no `applied` list, and the exit status stays nonzero. #446's ownership fix is not part of this | Failed entries carry no error; text prints only `FAIL secret` | [`windows/446`](windows/446), [`linux/446`](linux/446) |

The comment posted on each issue lists every checklist item with what ran and what was observed. The `.json` outputs hold every check with its observed values; the `.log` files hold the console output.

## Layout

- `probes/`:
  - the shared provenance, setup and release-source scripts;
  - for each issue: `probe_<n>*.py`, `steps_<n>.sh` (runner steps) and `run_<n>_windows.sh`;
  - `probe_449_lib.py` is a byte copy of the earlier `probe_420_lib.py` and keeps its CRLF line endings.
- `windows/<n>/`: the Windows outputs and `probe-sha256.txt`, with `control-0.1.14/` for the control.
- `linux/<n>/`: the runner's outputs, `run-id.txt` and the run log. The 0.1.14 control files end in `_0114`.
- `release-source/`: `release_source.json` and `.log`, and `prs.json` (each PR's merge commit, head and files, from the GitHub API).

Local paths are replaced by `<WORK>`, `<VENV>` and similar placeholders. Files are stored byte-for-byte (`.gitattributes`: `* -text`).
