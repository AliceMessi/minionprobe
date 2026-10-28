# OpenMinion #448: a turn's own capture asks the memory.write check, the fix

Validation of the PR for [openminion#448](https://github.com/openminion/openminion/issues/448), as proposed there ([comment](https://github.com/openminion/openminion/issues/448#issuecomment-6460713487)) and approved ([comment](https://github.com/openminion/openminion/issues/448#issuecomment-6465004554)). Branch `fix/448-capture-authorization`:
- three commits (test, fix, docs and changelog) on `dev` @ `042e0a2c`, ending at `e8c7e811`;
- a typing follow-up, `6dfae707`, for one new mypy ratchet error (`Returning Any` in `_memory_capture_authorized`);
- a merge of `dev` @ `63ac9a17` (#457), giving the validated head `8dc45d7b`.

A turn's auto fact extraction and its capture bundle write ran without a policy decision, so with a loaded `NEVER write memory records` identity constraint an ordinary SDK turn wrote its capture. With the fix, the brain bridge asks once per turn the check that pending-capture recovery asks (`memory_capture_allowed`, sharing `memory_capture_recovery_allowed`'s evaluation: the same internal actor and `memory.write` tool profile), with the turn's channel, the target `memory-capture`, its runtime session and its root turn. When the write is not allowed the turn makes no extraction request and writes nothing for its capture; its intent stays pending with `CAPTURE_RECOVERY_UNAUTHORIZED` until recovery's own check allows it.

The report itself is in [`openminion-terminal-capture-authorization`](../openminion-terminal-capture-authorization).

## Trees

| Name | Commit | What it is |
|---|---|---|
| `dev-042e0a2c` | `openminion/openminion@042e0a2c` | `dev` the PR's commits are on |
| `pr-e8c7e811` | `AliceMessi/openminion@e8c7e811` | the PR's three commits on it |
| `dev-63ac9a17` | `openminion/openminion@63ac9a17` | `dev` with #457 |
| `merged-8dc45d7b` | `AliceMessi/openminion@8dc45d7b` | the validated head: `e8c7e811`, the typing follow-up `6dfae707` and a merge of `dev-63ac9a17` |

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes.

- `probes/probe_capture_authorization.py`, `probes/probe_420_lib.py` and `probes/_common.py` are the report's probe and helpers, unchanged (same sha256 as in the report's `windows/probe-sha256.txt`). The probe's checks describe the reported behavior, so three of them fail on the PR's tree.
- `probes/probe_448_turn_capture.py` is the PR probe. It uses the same helpers and setup (a real `APIRuntime`, the `openai` provider pointed at a scripted OpenAI-compatible server on 127.0.0.1, an identity profile under `identity.bundle_root`) and adds a call-through recorder for every `memory.write` decision of the agent's security policy:
  - W, `NEVER write memory records`: one SDK turn with an approval callback that would approve, the recovery gate for the same capture, and one memory consolidation run through the daemon's TaskManager and cron scheduler;
  - R, the same runtime home reopened with `NEVER reveal secrets`: two more runs of the same job;
  - C, `NEVER reveal secrets` (control): one SDK turn.

  It has 12 checks.
- `probes/linux_setup.sh`: the runner script. It installs the four trees from git into fresh venvs (`uv venv --python 3.12`) and runs both probes with `python -I -X utf8` from an empty directory on each.
- `windows/run_probes.sh`: the same on native Windows 11 Pro 10.0.22631 (CPython 3.12.11, SQLite 3.50.4), with fresh `uv` venvs for `dev-042e0a2c` and `pr-e8c7e811`. The disk filled up before the venvs for `dev-63ac9a17` and `merged-8dc45d7b` could be installed, so:
  - `windows/probes/<tree>/` holds the outputs, `pip-list.txt` and `direct_url.json` (the installed commit) for the first two trees;
  - `windows/probes/worktree-8dc45d7b/` holds both probes run with the worktree's editable venv at `8dc45d7b` (`worktree-head.txt`);
  - `dev-63ac9a17` was not run on Windows.
  - `windows/probe-sha256.txt` has the hashes of the probe files that ran.
- `windows/pytest-merged-8dc45d7b.txt`, `windows/pytest-head-e8c7e811.txt`, `windows/pytest-dev-63ac9a17-sources.txt` and `windows/pytest-dev-042e0a2c-sources.txt`: the focused tests on native Windows, on the worktree and on a `git archive` of each `dev` with the branch's three test files (`PYTHONPATH=src`; the log prints the imported `openminion.__file__`). `windows/windows-env-and-ruff.txt` has the versions and `ruff check` / `ruff format --check`.
- `linux/probes/`: [run 40699636554](https://github.com/AliceMessi/linux-runner/actions/runs/40699636554), both probes on the four trees (GitHub-hosted Ubuntu 24.04, CPython 3.12.15, SQLite 3.45.1). `spec_*.txt` are the installed specs, and `probes.sha256` the hashes of the shipped probes, the same as on Windows.
  An earlier run on the first two trees only, [run 40692303961](https://github.com/AliceMessi/linux-runner/actions/runs/40692303961), gave the same results.
- `linux/gates-run-40699459658.txt`: [run 40699459658](https://github.com/AliceMessi/linux-runner/actions/runs/40699459658) on `8dc45d7b`, with the script `linux/runner-gates-merged.sh`. CPython 3.11.17, SQLite 3.45.1, the `postgres` extra and a PostgreSQL 16 service. The steps:
  - `make lint`;
  - on the head, the upstream `postgres-memory-retrieval` job's command list, read from `.github/workflows/ci.yml`;
  - the focused tests and a related slice, on the head and again after `git checkout 63ac9a17 -- src`;
  - back on the head, the 23 new tests 5 times, `ruff check .`, `make format-check`, `make workflow-check` and `make test-critical`.
- `linux/test-ci-run-40699517411.txt`: `make test-ci` on `8dc45d7b` ([run 40699517411](https://github.com/AliceMessi/linux-runner/actions/runs/40699517411)), with `linux/runner-test-ci-merged.sh`. Only its summary lines are kept, read from the job log, because the disk was full when the logs artifact was downloaded.
- `linux/earlier-e8c7e811/`: the same gates ([run 40691829158](https://github.com/AliceMessi/linux-runner/actions/runs/40691829158)) and `make test-ci` ([run 40691877209](https://github.com/AliceMessi/linux-runner/actions/runs/40691877209)) on `e8c7e811` against `042e0a2c` sources. Its `make lint` failed the mypy ratchet, 4310 / 4309. `runner-mypy-diff.sh` ([run 40698947929](https://github.com/AliceMessi/linux-runner/actions/runs/40698947929)) runs the ratchet's mypy on that head and on `dev` sources and prints the one new error, which `6dfae707` removes.

Paths are shortened to `<VENV>`, `<WORK>`, `<WORKTREE>`, `<DEV_TREE>`, `<RUNNER_TEMP>` and `<RUNNER_CHECKOUT>`. Timestamped log lines from the package are left out of the pytest, gates and test-ci logs.

## Results

Both probes give the same results on Windows and on Linux. The two `dev` trees give the same results, and so do the two PR trees.

| `NEVER write memory records`, one SDK turn (W) | `dev-042e0a2c`, `dev-63ac9a17` | `pr-e8c7e811`, `merged-8dc45d7b` |
|---|---|---|
| Provider requests in the turn | extraction, decide, freshness, freshness | decide, freshness, freshness |
| `memory.write` decisions in the turn | none | 1: `console` / `memory-capture` / `capture` / root turn, `require_approval`, `identity_hard_constraint_restricted` |
| Bundle writes, capture candidates, receipts, results | 1, 1, 1, 1 | 0, 0, 0, 0 |
| `memory_capture_state`, bundle result | `succeeded` | `pending`, `CAPTURE_RECOVERY_UNAUTHORIZED` |
| Reply | `openminion: Noted: tabs over spaces.` | the same |
| Approval callback calls (the callback would approve) | 0 | 0 |
| Recovery gate for the same capture | `False` (`require_approval`) | `False`, the same decision and actor scopes |
| First consolidation run | `(0, 0, 0)`: nothing pending | `(1, 0, 1)`, no extractor call |
| R: two more runs | `(0, 0, 0)`, `(0, 0, 0)` | `(1, 1, 0)` with one extractor call, then `(0, 0, 0)`; one result |
| C, `NEVER reveal secrets` | extraction, `succeeded`, no decision | extraction, `succeeded`, one `allow` decision with the turn's context |
| PR probe checks | 5/12 | 12/12 |
| Report probe checks | 9/9 | 6/9 |

On both trees the W turn's memory store also holds one `decision` record (`Decision: respond`), written by the brain's decision memory, not by the capture; the PR does not change it.

Tests (the 23 new tests: 11 SDK tests in `tests/runtime/test_turn_capture_authorization.py`, 12 owner tests in `tests/memory/runtime/test_capture_authorization.py`):

| | runner, head | runner, `dev` sources | Windows, head | Windows, `dev` sources |
|---|---|---|---|---|
| `<focused>` on `8dc45d7b` / `63ac9a17` | 192 passed | 21 failed, 171 passed | 187 passed, 5 failed | 26 failed, 166 passed |
| `<focused>` on `e8c7e811` / `042e0a2c` | 192 passed | 21 failed, 171 passed | 187 passed, 5 failed | 26 failed, 166 passed |
| related slice on `8dc45d7b` / `63ac9a17` | 8328 passed | 21 failed, 8307 passed | | |

The 21 are the new tests that expect the fix. The 5 Windows failures are `tests/test_doctor_command.py` cases that fail on this machine on every tree (`WinError 32` on the temporary `telemetry.db`) and pass on the runner.
