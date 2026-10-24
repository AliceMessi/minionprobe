# OpenMinion: PostgreSQL retention and purge on items with evidence

Left out of PR #399 (for #392) and reported separately, as the maintainer asked on #392. `probe_pg_evidence_refs.py` runs each retention step (`apply_confidence_decay`, `compress_old_summaries`, `enforce_scope_capacity`, `evict_stale_insights`, `purge_soft_deleted` on records and on candidates) on a bare store, with three items to remove: one with an artifact ref, one with the capture evidence entry that `apply_capture_bundle` writes, and one with no evidence. It runs on SQLite (the control) and on PostgreSQL, and prints JSON.

Run from an OpenMinion source checkout with development dependencies: `PYTHONPATH=src:. python probe_pg_evidence_refs.py` on Linux, `PYTHONPATH="src;." python probe_pg_evidence_refs.py` on Windows. The PostgreSQL cases need `OPENMINION_TEST_POSTGRES_URL` and the `postgres` extra; without it they are skipped.

Raw outputs (same probe, sha256 `2b1c3fe5948b3176d2642081a124554c905f2dd49ada32f61122aecba7e72020`):
- `run-linux-f349cd07.log`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17, PostgreSQL 16 (AliceMessi/linux-runner run 40109033202). First on `dev` @ `f349cd07`, then on the same checkout with `src` from PR #399 head `fdcf4de7`; the two outputs are identical.
- `run-windows-f349cd07.log`: native Windows 11, CPython 3.11.11, `dev` @ `f349cd07` (SQLite control only, identical to the Linux SQLite rows).
