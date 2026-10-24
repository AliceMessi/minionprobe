# OpenMinion: `memctl gc` with a PostgreSQL memory store

Left out of PR #399 (for #392) and reported separately, as the maintainer asked on #392. `probe_gc_purge_pg.py` runs the real `memctl gc` command (`python -m openminion.modules.memory gc`) with a memctl config file that selects SQLite (the control) or PostgreSQL. It then calls `run_gc` as `memctl gc` does, on the `AuditedMemoryStore` that `_get_service` builds, and on the `PostgresMemoryStore` inside it. After each case it reads the rows back and prints JSON.

Run from an OpenMinion source checkout with development dependencies: `PYTHONPATH=src:. python probe_gc_purge_pg.py` on Linux, `PYTHONPATH="src;." python probe_gc_purge_pg.py` on Windows. The PostgreSQL cases need `OPENMINION_TEST_POSTGRES_URL` and the `postgres` extra; without it they are skipped.

Raw outputs (same probe, sha256 `adeeb6a0d3acdfb324d086e52a184db37933a3c4fe0a9125a92fbeaae7e0cb4d`):
- `run-linux-f349cd07.log`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17, PostgreSQL 16 (AliceMessi/linux-runner run 40109033202). First on `dev` @ `f349cd07`, then on the same checkout with `src` from PR #399 head `fdcf4de7`. The log ends with the PostgreSQL truth-maintenance test on both trees, from run 40108810331.
- `run-windows-f349cd07.log`: native Windows 11, CPython 3.11.11, `dev` @ `f349cd07` (SQLite control only).
