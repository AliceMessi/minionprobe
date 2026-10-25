# OpenMinion: consolidation cron runs fail when a capture intent is pending

Found while testing the fix for [#397](https://github.com/openminion/openminion/issues/397). `probe_cron_capture_recovery.py` (sha256 `415e2755eaa9118e75a47bbabc7d5df1e67066f272523162f687940965731563`) runs one SDK turn (echo provider) with the bundled `reference-sqlite` external adapter, which leaves one pending terminal capture intent, then a memory consolidation job through the real `CronTurnExecutor.execute` on the same `APIRuntime`, and last the same recovery call with the extractor's arguments passed by keyword.

Files:
- `run-linux.log`, from `run_cron.sh`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17, sophiagraph 0.0.12 (AliceMessi/linux-runner run 40207501168), on `dev` sources (`b670bbb6`) and on the #397 fix branch `fix/397-external-capture-unsupported` @ `62a8819c`.
- `windows.log`: native Windows 11, CPython 3.11.11, the same two trees.

On both: `CronTurnExecutor.execute` raises `TypeError: BrainBridgeService.extract_memory_capture_candidates() takes 1 positional argument but 5 were given` from `capture_recovery.py:143`, and with keyword arguments the extractor fails with `TypeError: SessionStore.append_event() got an unexpected keyword argument 'actor_type'`.
