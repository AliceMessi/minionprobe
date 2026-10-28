# OpenMinion #456: a scheduled consolidation turn gets its job's context and skips its own capture, the fix

Validation of the PR for [openminion#456](https://github.com/openminion/openminion/issues/456), option (a) as approved there ([comment](https://github.com/openminion/openminion/issues/456#issuecomment-6465005607)). Branch `fix/456-consolidation-marker` @ `2145c61f` (3 commits on `dev` @ `39c36a1b`):
- `988503a5` test: the new tests, which fail on `dev`;
- `ecb40ea0` fix;
- `2145c61f` changelog.

The report and its reproduction are in [`openminion-consolidation-self-capture`](../openminion-consolidation-self-capture).

**The fix.** The cron executor attaches a consolidation job's fields to its own turn request as a typed `MemoryConsolidationTurnContext` (`trusted_consolidation_context`). They no longer go in the request meta.
- The runtime manager keeps the field.
- Runtime ingress copies it from the manager request and merges its five `memory_consolidation_*` keys into the inbound metadata after public parsing, as it does for the trusted subagent context.
- The gateway's prompt-time allowlist keeps those keys.
- Ingress drops `memory_consolidation_*` keys from a caller's `meta` and `inbound_metadata`.

## Files

| sha256 | file |
|---|---|
| `33dde7cb4968756d67da49c158897d429c16c505d2586d07b81d4c6345a37e5c` | `probes/_common.py` |
| `6a00f9f700579801e8fb26b128fdb8babe134ca410fabc50648336f3874052dd` | `probes/probe_420_lib.py` |
| `3188af226a913a154233a51eedb6e9869ece4faaae785a8eb6ac03d44dd4825f` | `probes/probe_consolidation_self_capture.py` |
| `9d9d0df8387004f093e22aaa54e1c54e1e3a0c6cf5a24bba1195383b9c8a6d59` | `probes/probe_consolidation_fix.py` |
| `d9af6299791023cfd4d82e4df6087f4485669e41f4d424e9e0a2f8a647f3f26f` | `tests/test_cron_consolidation_turn.py` |
| `3de0de05de1461e9ba352270eb03468cf2faa4e4d4fdb1e7b2ecf186ea9ae86e` | `tests/test_runtime_ingress_contract.py` |

`SHA256SUMS` lists every file in this folder.

- `probes/probe_consolidation_self_capture.py` is the issue's probe, unchanged. `_common.py` and `probe_420_lib.py` are its helpers, byte-identical to the issue's copies.
- `probes/probe_consolidation_fix.py` uses the same setup and states the fixed behavior. It runs four turns on one runtime:
  - **C:** the issue's caller control, `memory_consolidation_job: "true"` in `meta`.
  - **K:** a real consolidation cron run. The installed `task.consolidate_memory` handler creates the job through the daemon's `TaskManager`, and the daemon's own `attach_cron_scheduler` runs it.
  - **F:** an ordinary turn whose `meta` and `inbound_metadata` carry every `memory_consolidation_*` key, with K's real `cron_job_id`, `cron_run_id` and `scheduled_for`.
  - **O:** an ordinary (non-consolidation) cron job whose payload `inbound_metadata` carries the same keys.

  For each turn it records:
  - the keys and the typed field at the manager request and at the ingress request;
  - the brain message metadata that `capture_is_excluded` receives;
  - the consolidation context the brain attaches;
  - the capture intent, the bundle write and the provider's extraction requests.
- Both probes run with `python -I -X utf8` from an empty directory, on `openminion` installed from git into a fresh venv:
  - `dev`: `git+https://github.com/openminion/openminion@39c36a1b5183ca811a9031cbcb397b3f9470a06e`;
  - head: `git+https://github.com/AliceMessi/openminion@2145c61f14aa01bbcf4af4d2400f04d9a898ef10`.

  The provider is a scripted OpenAI-compatible server on 127.0.0.1.
- `tests/` are the new test file and the extended ingress test file as committed at `2145c61f`, for reference.

## Results

### Probes (installed packages, Linux and Windows give the same checks)

| | `dev` @ `39c36a1b` | head @ `2145c61f` |
|---|---|---|
| issue's probe (states the bug) | 10/10 pass: the bug reproduces | 4/10: the bug checks fail, as they should |
| fix probe (states the fix) | 3/11: only setup, the finished run and the offline provider pass | 11/11 pass |

K, the consolidation run:

| | `dev` | head |
|---|---|---|
| manager request | `memory_consolidation_*` in `meta`, no typed field | typed context `{target_scope: agent:openminion, batch_limit: 8, max_iterations: 2, timeout_seconds: 30}`; no consolidation key in `meta` |
| ingress request | the keys in its inbound metadata | no keys parsed from meta; the typed context is carried |
| brain message metadata (`capture_is_excluded` input) | no consolidation key; `False` | the five keys; `True` |
| consolidation context | not attached | attached once with the job's fields and 1 candidate (the one C's capture proposed) |
| extraction requests by the turn | 1 | 0 |
| intent, bundle | `pending`, `succeeded_no_output` | `excluded`, no result, no bundle write |

C, F and O are captured on both trees: 1 extraction, intent `pending`, a `succeeded` result with 1 candidate. None is excluded and none gets a consolidation context.
- On `dev` their keys reach the ingress request and the gateway drops them. That is why the fix probe's C, F and O checks fail there.
- On the head ingress drops them, and F's and O's `cron_job_id` still reach the brain.

### Linux runner (AliceMessi/linux-runner, GitHub-hosted Ubuntu 24.04.5)

- `linux/gates-run-40668243253.txt`, from [run 40668243253](https://github.com/AliceMessi/linux-runner/actions/runs/40668243253) with `linux/runner-gates.sh`. CPython 3.11.17, SQLite 3.45.1, a fresh clone and `make dev-install`.
  - On the head: the focused tests and a related slice.
  - The same after `git checkout 39c36a1b -- src` and `git checkout 988503a5 -- tests` (the test commit's tests).
  - Then, back on the head: the new test file 10 more times, `ruff check .`, `make format-check`, `make workflow-check`, `make lint` and `make test-critical`.
- `linux/test-ci-run-40668274067.txt`: `make test-ci` on the head, from [run 40668274067](https://github.com/AliceMessi/linux-runner/actions/runs/40668274067) with `linux/runner-test-ci.sh`.
- `linux/probes-run-40669536260/`: both probes on both trees, from [run 40669536260](https://github.com/AliceMessi/linux-runner/actions/runs/40669536260) with `linux/runner-probes.sh`. It holds each probe's `.log` and `.json`, the specs, the `pip list` of each venv, and `probes.sha256` for the shipped probe bytes. CPython 3.12.3, SQLite 3.45.1.

On the head:
- `pytest -q <focused>`: 81 passed. On `39c36a1b` sources with the test commit's tests: 6 failed (the three consolidation tests and the three ingress cases), 74 passed.
- The related slice under the `make test-ci` markers: 3010 passed, 1 xfailed. On `39c36a1b` sources: the same 6 failed, 3003 passed, 1 xfailed.
- `tests/runtime/test_cron_consolidation_turn.py` 10 more times: 5 passed each time.
- `ruff check .`, `make format-check` (5131 files), `make workflow-check`, `make lint` (mypy ratchet 4309 / 4309) and `make test-critical` (486 passed, 4 skipped) all pass.
- `make test-ci`: 20428 passed, 29 skipped, 1 xfailed, no failure. The `postgres` extra is not installed there.

### Native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4

- `windows/pytest-head-2145c61f.txt`: the focused tests on the worktree at the head. 81 passed.
- `windows/pytest-dev-39c36a1b-sources.txt`: the same files on a `git archive` of `39c36a1b`'s `src` with the test commit's `tests`. 6 failed, 74 passed:
  - the three consolidation tests;
  - the three ingress cases.

  Both ran through `windows/wrun.py`, which runs pytest and then prints the `openminion` module that pytest imported.
- `windows/ruff-head-2145c61f.txt`: `ruff check` and `ruff format --check` on the changed files, and `ruff check .`; all pass.
- `windows/probes-dev/` and `windows/probes-head/`: both probes on each tree, each from a fresh `uv` venv installed from git. They also hold `pip-list.txt`, `pip-show.txt` and `direct_url.json` with the commit.

Paths are shortened to `<VENV>`, `<WORK>`, `<EMPTY>`, `<PROBES>`, `<WORKTREE>`, `<DEV_TREE>`, `<SCRATCH>`, `<RUNNER_TEMP>` and `<CHECKOUT>`.
