# OpenMinion: `memctl inspect` without `--scope` fails with an uncaught `InvalidArgumentError`

Disclosed under "Not included" in #402; the maintainer asked for its own reproduction. This folder is that reproduction, on `dev` @ `8b220e14`.

`build_inspect_payload` (`modules/memory/diagnostics/cli.py:170-173`) runs `MemoryScope.parse(str(scope or "").strip())` and falls back to the default ids on `except ValueError`. `MemoryScope` is sophiagraph's, and with no `--scope` it raises `sophiagraph.contracts.errors.InvalidArgumentError: invalid scope: ''`, which derives from `MemctlError` and `RuntimeError`, not `ValueError` (sophiagraph 0.0.12). The exception reaches the user as a traceback and memctl exits 1.

Files:
- `probe_inspect_scope.py` (sha256 `018563278ec9de76c3612adcf9e876175a30d9117b3f84e84686bd9b1a39be53`): seeds a SQLite memory database with one fact, then runs `memctl inspect` as separate processes (`python -m openminion.modules.memory`) with no `--scope`, `--scope ""`, `--scope agent:a` (control) and `--scope not-a-scope`, with and without `--json`, plus `memctl stats` with no `--scope` (control). It prints each exit code and the last stderr line.
- `probe-windows-8b220e14.log`: native Windows 11, CPython 3.11.11, sophiagraph 0.0.12, a clean worktree of `8b220e14`; 3 identical runs, then the traceback of the first case (the openminion and sophiagraph frames).
- `probe-linux-8b220e14.log`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17, sophiagraph 0.0.12, AliceMessi/linux-runner run 40191238041, with `src/` reset to `8b220e14`. Identical output.

Results, on both platforms: no `--scope` and `--scope ""` exit 1 with `InvalidArgumentError: invalid scope: ''`, with and without `--json`. `--scope not-a-scope` exits 1 with the same exception for `'not-a-scope'`. `--scope agent:a` exits 0, and `memctl stats` without `--scope` exits 0 and counts the record.

Paths in the logs are shortened to `<tmp>`, `<checkout>` and `<site-packages>`.

Reproduce from an OpenMinion source checkout: `PYTHONPATH="src;." python probe_inspect_scope.py` on Windows, `PYTHONPATH=src:. python probe_inspect_scope.py` on Linux.
