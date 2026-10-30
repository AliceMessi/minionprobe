# OpenMinion #541: `Agent.run(..., thread_id=...)` answers a waiting confirmation

Evidence for the #541 PR (branch `fix/541-agent-run-explicit-thread` on `AliceMessi/openminion`, head `7d7a2ec4`): the explicit `thread_id` path, as approved on [openminion#541](https://github.com/openminion/openminion/issues/541#issuecomment-6522896997). The branch is three commits on `dev` @ `1169bd22` (after #536): the tests, the change and the docs. The report itself is in [`openminion-agent-run-confirmation-resume`](../openminion-agent-run-confirmation-resume).

| Commit | |
|---|---|
| `fcded075` `test(api)` | `tests/runtime/test_agent_explicit_thread.py` (new, 14 cases), 12 cases in `tests/api/test_agent.py`, an optional `extra_agents` argument for the #448 `_runtime` harness, the SDK typing fixture |
| `0332e143` `feat(api)` | `src/openminion/api/agent.py`; `max_callable_loc` 93 -> 96 in `scripts/baselines/api_complexity.tsv` (`_run_once` is the API package's longest callable and gains 3 lines) |
| `7d7a2ec4` `docs(api)` | `docs/runtime-surfaces.md`, `API_COMPATIBILITY.md`, `CHANGELOG.md` (Unreleased) |

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt); `sha256sum -c SHA256SUMS.txt` checks them. The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes. Runner paths are shortened to `<checkout>`; there are no local paths.

- `linux/runner-gates.sh`: the gates script, passed to the runner as base64 and run from the branch checkout (`echo <base64> | base64 -d > /tmp/runner-gates.sh && sha256sum /tmp/runner-gates.sh && bash /tmp/runner-gates.sh`; the hash is the log's first line).
- `linux/gates-run-41060921091.txt`, `head.txt`, `result.txt`, `run-id.txt`: its output on the final head ([run 41060921091](https://github.com/AliceMessi/linux-runner/actions/runs/41060921091)).
- `linux/earlier-012f74b9/`: [run 41058446529](https://github.com/AliceMessi/linux-runner/actions/runs/41058446529) on the first local head `012f74b9` (`wip/541-r1`) with `runner-gates-r1.sh`. Same source, tests and docs as the final head, without the ratchet change: everything passed except `make lint` and `tests/api/test_api_layout_validator.py` (2 cases), both on `API complexity increased: max_callable_loc 93 -> 96`. The final head is that tree plus the ratchet row, with the feat and docs commits made again on top of `fcded075`.
- `windows/windows-run.txt`: the new and changed test files and the #448 harness consumers on native Windows.
- `explore/test_explore_541.py`, `explore/explore-windows.txt`: the exploration run before the tests were written (Windows, on the change), one `OBS` line per call with its session, thread, routing decision, `respond_kind`, pending command and the handler's runs so far. It is not part of the PR.

## Gates on the final head

[Run 41060921091](https://github.com/AliceMessi/linux-runner/actions/runs/41060921091): GitHub-hosted Ubuntu 24.04.5, CPython 3.11.17, SQLite 3.45.1, a fresh clone of `7d7a2ec4` with `make dev-install`, the `postgres` extra and a PostgreSQL 16 service. "dev's sources" is `git checkout 1169bd22 -- src` in the same checkout (1 path differs: `api/agent.py`).

| Gate | `7d7a2ec4` | `dev`'s sources |
|---|---|---|
| `make lint` (mypy ratchet 4306 / 4306, layout validators) | passed | |
| `max_file_loc.py`, `scripts.validate.api_layout`, `scripts.validate.cli_layout` | ok, ok (`max_callable_loc` 96 / 96), ok | |
| `tests/runtime/test_agent_explicit_thread.py`, 3 runs | 14 passed each time | |
| new and changed test files | (in focused) | 25 failed, 38 passed |
| `pytest <focused>` | 164 passed, 1 xfailed | 25 failed, 139 passed, 1 xfailed |
| `pytest -m "<test-ci markers>" tests/api tests/runtime tests/brain tests/services tests/session tests/policy` | 7124 passed, 3 skipped, 11 deselected, 1 xfailed | |
| installed wheel SDK typing (the `ci.yml` step) | valid fixture: no issues; invalid fixture: its one expected error | |
| `ruff check .`, `make format-check` | passed, 5167 files already formatted | |

`<focused>` = the two new and changed files plus `tests/api/test_api_layout_validator.py tests/runtime/test_turn_capture_authorization.py tests/runtime/test_turn_denied_call_outcome.py tests/runtime/test_turn_optional_memory_writes.py tests/api/test_agent_sessions.py tests/api/test_tool_controls.py tests/api/test_public_surface.py tests/api/test_turn_api_runtime_facade.py`.

The 25 failures on `dev`'s sources are the new tests, the same set in both runs and in the focused slice:
- 11 runtime tests that read `first.thread_id`: `AttributeError: 'AgentRunResult' object has no attribute 'thread_id'`, before they reach `run(..., thread_id=...)`;
- the default-routing control with `""` and `"  "`, and 8 payload unit tests: `TypeError: Agent.run() / Agent.run_stream() got an unexpected keyword argument 'thread_id'`;
- the field-order unit test (`AssertionError`: there is no `thread_id` field) and the 3 metadata unit tests (`AttributeError`).

The default-routing control without the argument (`[omitted]`) passes on `dev`'s sources: a plain second `run("yes")` forks (`settled_without_resume`) and the waiting call does not run, as reported.

## What the isolation cases show

The routing resumes a requested thread id in the caller's session (`resume_thread` / `explicit_thread_requested`), and the brain keys its working state by `<runtime session>::conv:<thread>` (`services/brain/post_execution/context.py:53-67`). A thread id sent with another session id therefore finds no pending confirmation there, and the model answers "yes" as a new request: no waiting call ran in any crossed case, and each session's own reply still ran its call once. Another configured agent, named with `agent_id` as in the test, is refused in the owner's session (`TurnRequestError`, "Session ... belongs to agent 'openminion'", `services/runtime/ingress/execution.py:167-177`), and in its own session the owner's thread id runs nothing. No case executed another session's or agent's call, so no validation was added.

## Windows

`windows/windows-run.txt`: Windows 11 (10.0.22631), CPython 3.12.11, SQLite 3.50.4, a temporary `uv` venv with the head installed editable plus pytest (deleted afterwards). The new file and `tests/api/test_agent.py`: 63 passed; `test_turn_capture_authorization.py`, `test_turn_denied_call_outcome.py` and `test_turn_optional_memory_writes.py`: 27 passed.

## Not run

`make test-ci` in full, `make test-critical`, a live provider, the HTTP API and CLI surfaces.
