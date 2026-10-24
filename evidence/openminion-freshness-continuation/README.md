# OpenMinion: freshness obligation reset on a continuation step

Follow-up to #328, at the maintainer's request. `probe_freshness_continuation.py` rebuilds the trace's scripted-provider scenarios B and E on `dev` @ `f349cd07` and records the typed freshness state before and after every `_apply_entry_freshness` call, and the obligations and tool outputs at every closure freshness guard. Two controls: A1, a continuation whose entry call carries a valid `freshness` argument, and N1, a second turn with new user input.

The model is a scripted OpenAI-compatible fixture on 127.0.0.1 (no keys, no external network; non-loopback connections are refused and counted).

Run from an OpenMinion source checkout with development dependencies: `PYTHONPATH="src;." python probe_freshness_continuation.py` on Windows, `PYTHONPATH=src:. python probe_freshness_continuation.py` on Linux.

Raw outputs (same probe, sha256 `46f202591c702573d36a9674cb522c5e659f8ed844326adaeb068c838aa9c289`):
- `run-windows-f349cd07.log`: native Windows 11, CPython 3.11.11
- `run-linux-f349cd07.log`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17 (AliceMessi/linux-runner run 40103716426)

The scenario output is identical on both platforms.
