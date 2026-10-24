# OpenMinion #389: closing the bootstrap memory fallback

`probe_389.py` counts the memory store, audit sink and artifact store that the runtime memory assembly behind the bootstrap fallback creates. It closes each owner twice:
- a direct `build_agent_memory_service` caller;
- a gateway service built without `agent_memory`;
- a gateway service given the fallback memory as `agent_memory` (borrowed control);
- an echo SDK runtime (control).

With `OPENMINION_TEST_POSTGRES_URL` set, it repeats the two owners with a PostgreSQL memory store and also reports engine disposals and server connections. It prints JSON. `probe_fallback_retention.py` is the probe from [`openminion-gaps-380`](../openminion-gaps-380/), rerun on both trees.

Run from an OpenMinion source checkout with development dependencies: `PYTHONPATH="src;." python probe_389.py` on Windows, `PYTHONPATH=src:. python probe_389.py` on Linux.

Raw outputs, on `dev` @ `f349cd07` sources and on the branch `fix/389-fallback-memory-owner` @ `e015df93` ([PR #404](https://github.com/openminion/openminion/pull/404)):
- `probe_389-windows-{dev,branch}.txt` and `probe_fallback_retention-windows-{dev,branch}.txt`: native Windows 11, CPython 3.11.11.
- `run-linux-focus.log`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17, PostgreSQL 16. It starts with the sha256 of both probes. It covers:
  - the focused tests;
  - the PostgreSQL job;
  - both probes;
  - ruff, format and lint checks;
  - the services LOC ratchet;
  - `test-critical` on the branch.

  The same focused and PostgreSQL tests run on `dev` sources: the new tests fail there (5 focused, 2 PostgreSQL) and pass on the branch.
- `run-linux-testci-branch.log`: full `make test-ci` on the branch ([run 40108506470](https://github.com/AliceMessi/linux-runner/actions/runs/40108506470)), 20209 passed and 0 failed.
- `run-linux-testci-dev.log`: full `make test-ci` with the branch tests on `dev` sources ([run 40108544208](https://github.com/AliceMessi/linux-runner/actions/runs/40108544208)): 5 failed, all of them new tests (the log excerpt names four), 20204 passed.
