# Release verification of openminion 0.1.17 (installed package)

This folder covers the published-package checks the maintainer asked for on these openminion/openminion issues. Each issue is labelled `status:awaiting-release`, and each checklist is the maintainer's comment after the fix was merged:
- #446: [comment 6522959344](https://github.com/openminion/openminion/issues/446#issuecomment-6522959344) (fix #536);
- #456: [comment 6524247369](https://github.com/openminion/openminion/issues/456#issuecomment-6524247369) (guide and regression #535);
- #524: [comment 6521303587](https://github.com/openminion/openminion/issues/524#issuecomment-6521303587) (fix #534);
- #541: [comment 6526520086](https://github.com/openminion/openminion/issues/541#issuecomment-6526520086) (fix #546).

Everything below ran against `openminion==0.1.17` installed from PyPI into fresh virtual environments, not against a source checkout. The negative controls ran the same probes against `openminion==0.1.16` from PyPI in separate environments. The same probes also ran on `openminion==0.1.18` (Linux runner) as a secondary confirmation.

## Package provenance

| | |
|---|---|
| Install (Windows) | `uv venv --python 3.12` and `uv pip install --index-url https://pypi.org/simple "openminion==0.1.17"`; `openminion==0.1.16` the same way in a second venv for the controls |
| Install (Linux) | [`probes/linux_setup.sh`](probes/linux_setup.sh): `openminion[postgres]==0.1.17` (and `==0.1.16` for the controls) from PyPI on a GitHub-hosted runner with no openminion checkout; each run's `om-spec.txt` and `om-version.txt` record what was installed |
| Wheel | `openminion-0.1.17-py3-none-any.whl`, uploaded to PyPI 2026-11-02T07:08:57Z; GitHub release `v0.1.17` published 2026-11-02T07:15:13Z |
| sha256 | `84e50edf39044917df8f15d9ffb432af97a95ba5c6a88d5af60004a87bc9df2c` (PyPI JSON and downloaded wheel, on Windows and every Linux run) |
| Installed files | All 2762 hashed RECORD entries match the installed files (0 mismatched, 0 missing): [`windows/provenance/provenance-windows.json`](windows/provenance/provenance-windows.json) and each Linux run's `provenance-linux.json` |
| Windows | Native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4 |
| Linux | Ubuntu 24.04.5 GitHub-hosted runner, CPython 3.12.15 or 3.12.16 (recorded per run), SQLite 3.45.1, PostgreSQL 16.15 (`postgres:16`), SQLAlchemy 2.1.8, psycopg 3.3.8 |

## Release source

[`release-source/`](release-source/): the tag `v0.1.17` (annotated `df9f3747`, commit `1086d789`); the merges and reviewed heads of #534, #535, #536 and #546 are ancestors of it and of `v0.1.18` (`ancestry.txt`); all 2756 `openminion/` files of the wheel are byte-identical to `src/` at the tag; the sdist's tracked files equal the tag. `published-docs-v0.1.17/` holds byte copies of the docs the PRs touched (docs are not in the wheel), with their git blob ids.

## Layout

- `probes/`: every probe and runner step file, as run. Shared: `_common.py`, `provenance.py`, `linux_setup.sh`, `release_source.py`.
- `windows/<issue>/`: native Windows outputs (`<probe>.json` + `.log`), `probe-sha256.txt`, and `control-0.1.16/`.
- `linux/<issue>/`: the runner's outputs with `run-id.txt` (links to AliceMessi/linux-runner Actions runs) and `probe-sha256.txt`; `0.1.18/` subfolders for the secondary confirmation.

## Results

| Issue | 0.1.17 | 0.1.16 control | 0.1.18 |
|---|---|---|---|
| #446 | PARTIAL: main probe 116/118 (two failing cases reported on the issue), package 4/4, pooler 12/12, SQLite 13/13; upgrade matrix 1844 PASS, 39 REFUSED, 0 FAIL | main 80 of 103 checks fail | same as 0.1.17 |
| #456 | PASS: guide 20/20, intents 10/10, `probe_456` 19/19 on Windows and Linux | pass as expected (#535 changed docs and tests only); 0.1.14 as package under test: intents 3/10 | 20/20, 10/10, 19/19 |
| #524 | PASS: 24/24 (Windows SQLite), 24/24 (Linux SQLite), 22/22 (PostgreSQL); optional-writes harness 24/24, 24/24, 17/17 | 10/23, 9/21; 18/22, 12/15 | 23/23, 21/21, 22/22, 15/15 |
| #541 | PASS: 20/20 on Windows and Linux; typing passes | 1/18; typing fails | 18/18; typing passes |

Providers are local scripted servers on 127.0.0.1, not live models. All text outputs are sanitized (paths replaced by `<WORK>`, `<VENV>`, `<E>`); `.gitattributes` keeps files byte-for-byte. `SHA256SUMS.txt` lists every file.
