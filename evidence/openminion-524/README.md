# OpenMinion #524: a tool call that did not run stages no `tool_outcome` candidate

Evidence for the #524 fix PR (branch `fix/524-denied-call-outcome-candidate` on `AliceMessi/openminion`, head `8d688f7a`): option A, as approved on [openminion#524](https://github.com/openminion/openminion/issues/524#issuecomment-6494224192). The branch is three commits on `dev` @ `aec2df9e` (after #519, #525 and #520): the tests, the fix and the docs. The report itself is in [`openminion-user-denied-tool-args-candidate`](../openminion-user-denied-tool-args-candidate).

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes. Runner paths (`/home/runner/...`) are left as they are; there are no local paths.

- `linux/runner-gates.sh`: the gates script, passed to the runner as base64 and run from the branch checkout (`echo <base64> | base64 -d > /tmp/runner-gates.sh && bash /tmp/runner-gates.sh`).
- `linux/gates-run-40861759668.txt`: its output on the final head ([run 40861759668](https://github.com/AliceMessi/linux-runner/actions/runs/40861759668)); `linux/gates-head.txt` is the commit the runner checked out.
- `linux/check-run-40861340925.txt`, `linux/check-head.txt`: an earlier check of the new and changed tests ([run 40861340925](https://github.com/AliceMessi/linux-runner/actions/runs/40861340925)) on `wip/524-check1` @ `953dd9dc`, which has the same source and docs as the head and one test assertion that the head changed (below).
- `linux/explore/`: two exploration runs made before the tests were written, on `wip/` commits that add a throwaway test file to `dev` with the fix. `test_explore_524.py` (explore1, and the helpers explore4 imports) and `test_explore_524b.py` (explore4) are those files; they are not part of the PR. `explore1-run-40859577085.txt` and `explore4-run-40860841929.txt` are the runs' outputs, `explore*-head.txt` the commits, and `explore*-summary.txt` the output of `python -I summarize.py <run log>`, which prints each scenario's turns from the `OBS` lines.

## Gates on the final head

[Run 40861759668](https://github.com/AliceMessi/linux-runner/actions/runs/40861759668): GitHub-hosted Ubuntu 24.04, CPython 3.11.17, SQLite 3.45.1, a fresh clone of `8d688f7a` with `make dev-install` (`.deps/openminion-eval` from `dev`), the `postgres` extra and a PostgreSQL 16 service. "dev's sources" is `git checkout aec2df9e -- src` in the same checkout (2 paths differ: `tool.py` and the memory README).

| Gate | `8d688f7a` | `dev`'s sources |
|---|---|---|
| `make lint` (mypy ratchet 4306 / 4306, layout validators) | passed | |
| `scripts/validate/max_file_loc.py` | ok (`tool.py` stays at the 1000-line ceiling) | |
| upstream `postgres-memory-retrieval` job, 8 commands from `ci.yml` | 19, 1, 7, 6, 54, 8, 2, 13 passed | |
| `pytest <focused>` | 114 passed | 11 failed, 103 passed |
| `pytest -m "<test-ci markers>" <related>` | 8184 passed, 6 skipped, 1 xfailed | |
| new and changed test files, 3 runs | 56 passed each time | 11 failed, 45 passed |
| `ruff check .`, `make format-check` | passed, 5162 files already formatted | |

The 11 failures on `dev`'s sources are the new and changed tests, each for the reported reason:
- `test_call_that_did_not_run_stages_no_tool_outcome`, 4 cases (denied and waiting, `NEVER reveal secrets` and no identity profile): the `policy_denied` candidate with the call's arguments;
- `test_pending_call_approved_later_stages_once_after_it_runs`, 2 cases: the same candidate, already staged by the waiting turn;
- #525's `test_allowed_turn_writes_decision_records_as_before`, 3 cases: the `policy_denied` candidate for the denied call;
- `test_prepare_tool_dispatch_confirmation_stages_no_tool_outcome` and `test_tool_call_that_did_not_run_stages_no_tool_outcome` (unit): the candidates written.

The executed-call controls (`test_executed_call_keeps_its_tool_outcome`, 3 cases) and #525's gate unit test pass on both trees.

## Explorations

Both ran each scenario on the `wip/` commit's sources and then on `dev`'s sources (`DEVSRC` in the log). The scripted model calls `memory.write` once with `{"record_type": "fact", "key": "fact:editor_font", "title": "Editor font", "content": "The user writes code in Iosevka 14."}`.

**explore1** ([run 40859577085](https://github.com/AliceMessi/linux-runner/actions/runs/40859577085), `wip/524-explore` @ `8c9a9332` = `dev` + the one-line fix + `tests/runtime/test_explore_524.py`). Command: `pytest -q -s tests/runtime/test_explore_524.py`, then `git checkout aec2df9e -- src` and the same again. One `Agent.run` per scenario, with the run's new `memory_candidates` and `memory_records` rows:

| Scenario | `dev` | fix |
|---|---|---|
| denied by the callback (`NEVER reveal secrets`; no identity) | `tool_outcome:memory.write:policy_denied` with the arguments | none |
| waiting, no callback (both profiles) | the same | none |
| approved, succeeds | the record; no `tool_outcome` | the same |
| approved, the handler raises | `tool_outcome:memory.write:failure:EXEC_ERROR` with the arguments | the same |
| approved, the handler raises, `NEVER write memory records` | no `tool_outcome` (#525) | the same |

Its three "later yes" scenarios sent the reply with a second `Agent.run`, which forked a new thread, so the waiting call did not run; explore4 sends it on the first turn's thread.

**explore4** ([run 40860841929](https://github.com/AliceMessi/linux-runner/actions/runs/40860841929), `wip/524-explore4` @ `274d9a6f` = `dev` + the fix + the draft tests + both exploration files). Command: `pytest -q -s tests/runtime/test_explore_524b.py`, then the draft `tests/runtime/test_turn_denied_call_outcome.py` and `tests/runtime/test_turn_optional_memory_writes.py`, then the same on `dev`'s sources. A first `Agent.run` leaves the call waiting; the reply goes through `APIRuntime.run_turn` with the first turn's `thread_id`, except in `yes-agent-resume`:

| Reply | `dev` | fix |
|---|---|---|
| `yes` (with or without `forced_tools`), `allow_session` | waiting turn: `policy_denied` candidate; reply turn: the record, no candidate | waiting turn: none; reply turn: the record, no candidate |
| `yes`, the handler raises | waiting turn: `policy_denied`; reply turn: `failure:EXEC_ERROR` (two for one call) | waiting turn: none; reply turn: `failure:EXEC_ERROR` |
| `no` | waiting turn: `policy_denied`; reply turn: nothing | nothing |
| `yes` with a second `Agent.run` | `fork_thread` (`settled_without_resume`), the call does not run | the same |

The same run's draft SDK file gave 2 failed, 12 passed on the fix. The log keeps only the last 30 lines of that output; they show `test_executed_call_keeps_its_tool_outcome[failed-without-memory-write]` failing on the draft's comparison of the handler's arguments, which include their defaults, with the model's four. The tested file records only those four keys.

**check** ([run 40861340925](https://github.com/AliceMessi/linux-runner/actions/runs/40861340925), `wip/524-check1` @ `953dd9dc`): the three new and changed test files twice, `ruff format --check` and `ruff check` on them, then the files on `dev`'s sources. One case failed on the fix, twice: the later-approval test compared the failure candidate's signature with the model's arguments, while the replayed call carries its validated arguments with their defaults. The tested head compares the four keys and checks that `source_args_signature` equals `args_signature`. On `dev`'s sources: the same 11 failures as on the head.

## Not run

Windows, `make test-ci`, `make test-critical`, a live provider.
