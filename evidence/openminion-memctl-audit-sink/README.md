# OpenMinion: memctl leaves the memory audit database open

Side finding while working on #392. `probe_memctl_audit_sink.py` runs `memctl approve` in-process through typer's `CliRunner` with the cyclic garbage collector disabled. It lists the sqlite3 connections still open on the audit database, with the openminion frames that opened them, and tries to rename the file. It then runs the cyclic collector once to list the openminion objects it finds unreachable, and tries the rename and a delete again. Last, it runs the same command as a separate process and tries the rename and delete right after the process exits.

Run from an OpenMinion source checkout with development dependencies: `PYTHONPATH=src:. python probe_memctl_audit_sink.py` on Linux, `PYTHONPATH="src;." python probe_memctl_audit_sink.py` on Windows. The rename and delete results are only meaningful on Windows.

Raw outputs (same probe, sha256 `592c272d186c698fe2df0eb8a43a0edd1df8be200e63c11319c777c11f232814`):
- `run-windows-f349cd07.log`: native Windows 11, CPython 3.11.11, `dev` @ `f349cd07`. It ends with `tests/memory/test_cli_read_commands.py` on the same checkout (3 failures with `WinError 32` on `test.audit.db`).
- `run-linux-f349cd07.log`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17 (AliceMessi/linux-runner run 40108810331). It is the same except for the rename and delete results, and the test file gives 16 passed.
