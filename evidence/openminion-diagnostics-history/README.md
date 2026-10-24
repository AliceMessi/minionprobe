# OpenMinion: `memctl inspect` history summary with the audited store

Side finding while working on #392. `probe_inspect_history.py` seeds a SQLite memory database with a keyed fact that has two versions. It runs `memctl history` and `memctl inspect` as separate processes, then calls `build_inspect_payload` with the service memctl's `_get_service` builds and with a `MemoryService` over the bare `SQLiteMemoryStore`. It prints JSON.

Run from an OpenMinion source checkout with development dependencies: `PYTHONPATH=src:. python probe_inspect_history.py` on Linux, `PYTHONPATH="src;." python probe_inspect_history.py` on Windows.

Raw outputs (same probe, sha256 `c7ff0322423562ea2bc887faefbf77b79933d53997aedf931d885fbd552bfb34`):
- `run-windows-f349cd07.log`: native Windows 11, CPython 3.11.11, `dev` @ `f349cd07`
- `run-linux-f349cd07.log`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17 (AliceMessi/linux-runner run 40108810331)

The JSON output is identical on both platforms.
