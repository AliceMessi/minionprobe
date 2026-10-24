# OpenMinion: `run.` event-type case across run readers and backends

Follow-up to #352, at the maintainer's request. `probe_run_event_type_case.py` writes one session with `run.queued`, `run.running`, `RUN.failed`, `run.queued` and `Run.completed` events through `SessionStore.append_event` and prints what each run reader returns on SQLite and on PostgreSQL. It uses only APIs present in both openminion 0.1.10 and the current tree, so the same script gives the pre-#372 baseline.

Usage: `python probe_run_event_type_case.py <work_dir>`. The PostgreSQL rows need `OPENMINION_TEST_POSTGRES_URL` and the `postgres` extra (one new schema per run, dropped at the end); without it they are skipped.

Raw outputs (same probe, sha256 `d5f914fbda98f664e34f3bfd3e44f26628cb6438f45e8279e4878c6557ea807c`):
- `run-linux-f349cd07-and-0.1.10.log`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17, SQLite 3.45.1, PostgreSQL 16.15; `dev` @ `f349cd07` (editable checkout) and `openminion[postgres]==0.1.10` from PyPI in a separate venv (AliceMessi/linux-runner run 40103716221)
- `run-windows-f349cd07.log`, `run-windows-0.1.10.log`: native Windows 11, CPython 3.11.11, SQLite only; `dev` @ `f349cd07` and `openminion==0.1.10` from PyPI in a separate venv

On each backend the two versions print identical lines, and the SQLite lines are identical on both platforms.
