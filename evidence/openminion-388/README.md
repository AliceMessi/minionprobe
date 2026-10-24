# OpenMinion #388: external memory provider shutdown

`probe_388.py` builds an SDK runtime (echo provider, explicit roots) and closes it twice with the default memory provider (control) and with the `external` provider: the bundled `reference-sqlite` adapter (no `close()`), the same engine with a counting `close()`, an unusable `memory.store` setting, and, with `OPENMINION_TEST_POSTGRES_URL` set, a PostgreSQL `memory.store`. It prints JSON.

Run from an OpenMinion source checkout with development dependencies: `PYTHONPATH="src;." python probe_388.py` on Windows, `PYTHONPATH=src:. python probe_388.py` on Linux.

Raw outputs, each with the sha256 of the probe, on `dev` @ `f349cd07` sources and on the branch `fix/388-external-memory-close`:
- `run-windows.log`: native Windows 11, CPython 3.11.11
- `run-linux.log`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17, PostgreSQL 16
