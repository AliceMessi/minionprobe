# OpenMinion: external memory provider cannot run a turn

Side finding from #388, reported separately at the maintainer's request. `probe_external_capture.py` runs two SDK echo turns with the default memory provider (control) and with `memory.backend.provider: external` and the bundled `reference-sqlite` adapter on `dev` @ `f349cd07`. It reports each turn's run state or exception with the openminion frames of the capture path, what the capture path sees (service store, `apply_capture_bundle` availability), and the capture intents and results left in the session.

Run from an OpenMinion source checkout with development dependencies: `PYTHONPATH="src;." python probe_external_capture.py` on Windows, `PYTHONPATH=src:. python probe_external_capture.py` on Linux.

Raw outputs (same probe, sha256 `3cfac3d4e0d2e1edaccd1931d52ec29ee3fc01b4d15859ef3d5bbef3d6e96550`):
- `run-windows-f349cd07.log`: native Windows 11, CPython 3.11.11
- `run-linux-f349cd07.log`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17 (AliceMessi/linux-runner run 40103716426)

The JSON output is identical on both platforms.
