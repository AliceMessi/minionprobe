# OpenMinion: room active agent set from a stale read

The maintainer asked, on PR #385 (for #364), for the first-active-agent race that PR disclosed under "Left out" to be filed as a separate issue with a two-connection reproduction. `probe_race_active_agent.py` reproduces it on `dev` @ `f349cd07`.

`RuntimeSessionStoreParticipants.add_participant` commits the participant row, reads the session, and when it finds no active agent writes `active_agent_id` with an unconditional `UPDATE` in a separate statement. The probe opens two runtime storages on one database (SQLite file, or one PostgreSQL schema) and pauses one of them between that read and that write:

1. two first joins: both read no active agent; the later join's write wins;
2. an explicit `set_active_agent(beta)` made during a first join of `alpha` is overwritten with `alpha`;
3. `alpha` leaves during its own first join; the join raises `RuntimeError`, and `alpha`, no longer a participant, is stored as the active agent.

A fourth part runs two first joins concurrently, 200 rooms, with no pause.

Run from an OpenMinion source checkout: `PYTHONPATH=src python probe_race_active_agent.py [rooms]` (on Windows `set PYTHONPATH=src`). The PostgreSQL rows need `OPENMINION_TEST_POSTGRES_URL` and the `postgres` extra; without it they are skipped.

Raw outputs, three runs each, with the sha256 of the probe:
- `run-linux-f349cd07.log`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17, SQLite 3.45.1, PostgreSQL 16.15
- `run-windows-f349cd07.log`: native Windows 11, CPython 3.11.11, SQLite 3.47.1

Parts 1-3 give the same result on every run and backend.
