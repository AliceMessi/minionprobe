# OpenMinion 0.1.24: published-package verification of #717 and #446

`openminion==0.1.24` (PyPI, uploaded 2026-11-21T14:43Z; wheel sha256 `d93caac7…`, sdist sha256 `70799b1b…`) is the first release with both fixes:
- **#717**: PR #795 (merge `4204bbf0`, reviewed head `6c345231`), `Retry-After` admission within the turn deadline;
- **#446**: PR #817 by LBCron (merge `01e47b86`, contributor head `4cbc5e11`), PostgreSQL schema ownership and namespace collisions.

The negative control in both folders is the published 0.1.23, which predates both merges.

## Results

| Check | 0.1.24 | 0.1.23 (control) |
|---|---|---|
| `717/`: `probe_717.py`, Windows 11 and Linux | 19/19 PASS | 6/19 (the hint checks fail) |
| `717/`: `release_source_717.py` | 14/14 PASS | n/a |
| `446/`: `probe_446_0124.py`, Linux with PostgreSQL 16 | 23/25: every acceptance item passes; the 2 failing checks are the in-schema table race described in the report | 11/24 |
| `446/`: `release_source_446.py` | 13/13 PASS | n/a |

Linux runs:
- #717: https://github.com/AliceMessi/linux-runner/actions/runs/44984686686
- #446: https://github.com/AliceMessi/linux-runner/actions/runs/44984611059 (main) and https://github.com/AliceMessi/linux-runner/actions/runs/44984198401 (first round)

## Layout

Each of `717/` and `446/` has:
- `probes/`: the probe, its helpers, the Linux runner scripts and the release-source check;
- `linux/` and `windows/`: outputs on 0.1.24 and on the 0.1.23 control;
- `release-source/`: the release-source log and digests.

Offline: providers are scripted servers on 127.0.0.1, and PostgreSQL is the runner's `postgres:16` service. Local paths are replaced with tokens such as `<VENV>` and `<WORK>`.
