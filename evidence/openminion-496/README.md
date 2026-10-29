# OpenMinion #496: decision records and tool outcome candidates need the turn's memory.write answer

Evidence for the #496 fix PR (branch `fix/496-optional-memory-writes-policy` on `AliceMessi/openminion`, head `44a6e239`): option A for both writes, as approved on [openminion#496](https://github.com/openminion/openminion/issues/496#issuecomment-6485462294). The branch is stacked on the #471 head `6064d6d7`; its three commits are the test, the fix and the docs.

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes.

- `linux/runner-gates.sh`: the gates script, passed to the runner as base64 and run from the branch checkout.
- `linux/gates-run-40819905848.txt`: its output on the final head ([run 40819905848](https://github.com/AliceMessi/linux-runner/actions/runs/40819905848)); `linux/gates-head.txt` is the commit the runner checked out.
- `linux/earlier-02334926/`: the same gates on the earlier head `02334926` ([run 40818122002](https://github.com/AliceMessi/linux-runner/actions/runs/40818122002)) and the diagnosis of its one failure and format finding ([run 40819674408](https://github.com/AliceMessi/linux-runner/actions/runs/40819674408), `runner-diagnose.sh`). `44a6e239` differs from it only in `tests/runtime/test_turn_optional_memory_writes.py`.
- `probes/`: the #496 report's probe `probe_turn_memory_writes.py` (unchanged, same sha256 as in [`openminion-turn-memory-writes-under-constraint`](../openminion-turn-memory-writes-under-constraint)), the helpers `probe_420_lib.py` and `_common.py` (unchanged), the user-denied probe `probe_user_denied_tool_args.py` (the separate report, [`openminion-user-denied-tool-args-candidate`](../openminion-user-denied-tool-args-candidate)), and `linux_setup.sh`, which installs three trees into fresh venvs and runs both probes on each.
- `linux/probes/`: [run 40819905175](https://github.com/AliceMessi/linux-runner/actions/runs/40819905175) (`run-id.txt`), GitHub-hosted Ubuntu 24.04, CPython 3.12.15, SQLite 3.45.1. The runner checked out `AliceMessi/openminion@0956b52a`, which is `dev` @ `ce3b6771` plus a `probe_tmp/` folder with the five probe files and nothing else (`head.txt`), and ran `PR496=44a6e239f18b00e84a3e49f4fbc6ccca4f83578b bash probe_tmp/linux_setup.sh`. Per tree: both probes' JSON and log, `spec_<tree>.txt`, `pip-list_<tree>.txt`; `probes.sha256` has the bytes that ran; `run.log` is the job's output.

Paths are shortened to `<VENV>` and `<RUNNER_TEMP>`.

## Gates on the final head

[Run 40819905848](https://github.com/AliceMessi/linux-runner/actions/runs/40819905848): GitHub-hosted Ubuntu 24.04, CPython 3.11.17, SQLite 3.45.1, a fresh clone of `44a6e239` with `make dev-install` (`.deps/openminion-eval` from `dev`), the `postgres` extra and a PostgreSQL 16 service. "#471's sources" is `git checkout 6064d6d7 -- src` in the same checkout; "dev's sources" is `git checkout ce3b6771 -- src`.

| Gate | `44a6e239` | #471's sources | `dev`'s sources |
|---|---|---|---|
| `make lint` (mypy ratchet 4306 / 4306, layout validators) | passed | | |
| upstream `postgres-memory-retrieval` job, 8 commands | 19, 1, 7, 6, 51, 8, 1, 13 passed | | |
| `pytest <focused>` | 265 passed | 13 failed, 252 passed | |
| `pytest -m "<test-ci markers>" <related>` | 8382 passed, 6 skipped, 1 xfailed | 13 failed, 8369 passed | |
| `tests/runtime/test_turn_optional_memory_writes.py` | 7 passed (in the runs above) | 4 failed, 3 passed | 6 failed, 1 passed |
| new and changed test files, 5 runs | 79 passed each time | | |
| `ruff check .`, `make format-check` | passed, 5161 files already formatted | | |

The 13 failures on #471's sources are the new and changed tests: the denied, deny and approved SDK cases, the reused-runner test, the two decision-memory unit tests, the tool-outcome unit test, the four bridge unit tests (no `_turn_memory_write_allowed`), and the two #471 SDK tests that now expect an evaluation on excluded and disabled turns. The three allowed controls pass there.

## The #496 report's probe on three installed trees

Trees: `dev` @ `ce3b6771`, the #471 head `6064d6d7`, and this PR's head `44a6e239`, each installed from git into a fresh venv. Case W is `NEVER write memory records`, N `NEVER reveal secrets`, X no identity profile (see the report's README for the steps T1-T6 and C).

| | `dev-ce3b6771` | `pr471-6064d6d7` | `pr496-44a6e239` |
|---|---|---|---|
| W: `decision` records, T1-T6 and C | 7 (one per turn and the consolidation turn) | 7 | 0 |
| W: `tool_outcome` candidate for T3's denied call | 1, with the refused arguments | 1 | 0 |
| W: T3 approval callback | once, denied | the same | the same |
| W: `memory.write` decisions | none | `memory-capture` `require_approval` on T1-T6; recovery's in C | the same on T1-T6; in C recovery's and the consolidation turn's own `memory-capture` `require_approval` |
| W: turn capture, C recovery | `succeeded`; `(0, 0, 0)` | `pending`; `(5, 0, 5)` | `pending`; `(5, 0, 5)` |
| N, X: decision records, candidate | 7, 1 | 7, 1 | 7, 1 |
| N, X: `memory.write` decisions | none | `memory-capture` `allow` on T1-T6 | the same, and `allow` on the consolidation turn |
| Report probe checks | 11/11 | 11/11 | 3/11 |

On the PR head the probe's six checks of the reported W behavior fail as expected: the decision records, the T3 candidate, the decision-record read-back, the candidate in the consolidation batch, its refused promotion, and "not shown to later turns" (which needs a candidate to exist). The two control checks (N, X) also fail, but only on their last condition, that the controls land the same writes as W (`shape_equals_W: False`). Recomputed from the JSON, their other conditions hold: one decision record per turn from `write_decision_memory` and the T3 candidate with the denied arguments, as on the other trees. The identity, outcome feedback and offline checks pass.

The user-denied probe gives 9/9 on all three trees: under an allowed policy a call the user denies still stages its candidate with the arguments, which this PR does not change (see the separate report).
