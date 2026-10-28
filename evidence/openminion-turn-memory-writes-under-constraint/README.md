# OpenMinion: turn memory writes under `NEVER write memory records`

Evidence for the report split out of [openminion#471](https://github.com/openminion/openminion/pull/471) (the #448 fix) at the maintainer's request: the memory writes in a turn that are not the turn's own capture, with a normal SDK reproduction, a current-`dev` control, the #471 head and the published 0.1.14.

With a loaded `NEVER write memory records` identity constraint, `memory.write` is refused for the agent (recovery's gate returns `False`, the turn's tool adapter asks for confirmation, and on the #471 head the turn's capture stays pending). In the same turns, with no `memory.write` decision:
- **decision records:** every routed turn writes one `decision` record to `memory_records` (`write_decision_memory`);
- **`tool_outcome` candidate:** a denied model `memory.write` call stages a `tool_outcome` candidate in `memory_candidates`, in agent scope, with the refused arguments in its content. A consolidation run shows it to the model; the model's `promote` is refused by the promotion policy (source `agent_inferred`), so it stays `proposed`;
- **outcome feedback** (`_apply_typed_memory_outcome`): no call in any turn.

The same writes land with `NEVER reveal secrets` and with no identity profile (controls).

## Trees

| Name | Installed from | What it is |
|---|---|---|
| `dev-39da8ffb` | `openminion/openminion@39da8ffb` | current `dev` ("Merge pull request #466") |
| `pr448-8dc45d7b` | `AliceMessi/openminion@8dc45d7b` | the #471 head (`fix/448-capture-authorization`) |
| `pypi-0.1.14` | `openminion==0.1.14` from PyPI | the published package |

The two git trees also report `__version__` 0.1.14; `direct_url.json` in each JSON banner records the installed commit.

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes.

- `probes/probe_turn_memory_writes.py`: the probe. `probes/probe_420_lib.py` and `probes/_common.py` are the #448 probe's helpers, unchanged (same sha256 as in [`openminion-448`](../openminion-448)). Installed package only, no repo test fixtures; it runs with `python -I -X utf8` from an empty directory. 11 checks.
- `probes/linux_setup.sh`: the runner script. It installs the three trees into fresh venvs (`uv venv --python 3.12`) and runs the probe on each.
- `linux/`: [run 40738469866](https://github.com/AliceMessi/linux-runner/actions/runs/40738469866) (`run-id.txt`): GitHub-hosted Ubuntu 24.04, CPython 3.12.15, SQLite 3.45.1. The runner checked out `AliceMessi/openminion@7e97d65e`, which is `dev` @ `39da8ffb` plus a `probe_tmp/` folder with these four probe files and nothing else; it ran `bash probe_tmp/linux_setup.sh`. `probes.sha256` shows the probe bytes that ran (the same as here). Per tree: `probe_turn_memory_writes_<tree>.json` and `.log`, `spec_<tree>.txt`, `pip-list_<tree>.txt`; `run.log` is the job's output.
- `windows/pypi-0.1.14/`: the same probe on native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4, with the existing `openminion==0.1.14` venv from PyPI (`pip-list.txt`, the venv's distributions as `name==version`). Its outputs are stored with LF line endings. `windows/probe-sha256.txt` has the hashes of the probe files that ran.
- `probes/probe_420_lib.py` keeps the CRLF line endings it has in `openminion-448`, so its bytes and sha256 are the same; the runner checked out the same bytes (`linux/probes.sha256`).

Paths are shortened to `<VENV>`, `<WORK>` and `<RUNNER_TEMP>`.

## What the probe does

A real `APIRuntime` with the built-in SQLite memory store and one agent, `openminion`, whose `openai` provider points at a scripted OpenAI-compatible server on 127.0.0.1. Three cases, each a fresh runtime home: **W** identity profile with `NEVER write memory records`; **N** `NEVER reveal secrets`; **X** no identity profile. Every turn has an approval callback that records and denies.

| Step | Call | Message |
|---|---|---|
| T1 | `Agent.run(session_id="s1")` | "Please remember that I prefer tabs over spaces for indentation." |
| T2 | `Agent.run(session_id="s1")` | "Which indentation style do I prefer?" |
| T3 | `Agent(forced_tools=["memory.write"]).run(session_id="s1")` | the question; the model calls `memory_write` with `{"record_type": "fact", "key": "fact:indentation", "title": "Indentation", "content": "The user prefers tabs."}`, the callback denies |
| T4, T5 | `APIRuntime.run_turn`, session `s2`, `conversation_id` `chat` | "Say hello.", then the question |
| C | one memory consolidation run (installed `task.consolidate_memory` handler, daemon `TaskManager` and `attach_cron_scheduler`) | the job's message; the scripted model routes the turn into the act loop (entry `decompose`, no subtasks) and, if the request holds the `tool_outcome` candidate id, answers with a `memory_consolidation` trailer that promotes it |
| T6 | `Agent.run(session_id="s3")` | "Say hello." |

After T1 it asks recovery's gate (`memory_capture_recovery_allowed`) for T1's capture and the turn's tool adapter for `memory.write`. Call-through recorders (they return the original result) keep every `memory.write` decision of the agent's security policy; every `write_record`, `stage_candidate` and `apply_outcome_feedback` call on the brain's memory API with its caller frames; the consolidation signal parser's output and `apply_memory_consolidation_decisions`' arguments and result. The probe reads `memory_records` and `memory_candidates` before and after each step, and searches every provider request for the stored ids and the refused content.

## Results

Case W, `NEVER write memory records`:

| | `dev-39da8ffb` | `pr448-8dc45d7b` | `pypi-0.1.14` (Linux and Windows) |
|---|---|---|---|
| Recovery's gate for T1's capture | `False` (`require_approval`, `identity_hard_constraint_restricted`) | the same | the same |
| Turn's tool adapter, `memory.write` | not allowed, requires confirmation, `identity_hard_constraint_restricted` | the same | the same |
| Turn capture (T1 / T2-T6) | `succeeded` / `succeeded_no_output` | `pending` (each turn asks `memory-capture`: `require_approval`) | `succeeded` / `succeeded_no_output` |
| `decision` records written (T1-T6) | 6, one per turn: `Decision: respond` (T3: `Decision: act`), scope `session:<session>::conv:<id>`, via `write_decision_memory` | the same | the same |
| `memory.write` decisions asked for them | none | none (only the capture's `memory-capture`) | none |
| T3: approval callback | once, `memory.write` with the refused arguments, denied; no `fact:indentation` record | the same | the same |
| T3: `tool_outcome` candidate | 1: `agent:openminion`, `proposed`, `tool_outcome:memory.write:policy_denied`, source `agent_inferred`, `content.args_signature` = `{"content":"The user prefers tabs.","key":"fact:indentation","record_type":"fact","title":"Indentation"}` | the same | the same |
| Read-back of a decision record | by id: the next turn of the same conversation carries it (T5's entry request, and its extraction request, hold T4's id in the hydrated `decision_memory_refs`); no id across `Agent.run` calls or sessions; no request has a `[DECISION MEMORY]` section | the same (T5 makes no extraction request) | the same as `dev` |
| C: recovery | `(0, 0, 0)` | `(5, 0, 5)`, 5 `require_approval` | `(0, 0, 0)` |
| C: candidate batch shown to the model | yes: `2. id=<candidate> type=tool_outcome ...`, preview ending `'args_signature': '{"content":"The user prefers` | the same | no batch (the consolidation context is not attached on 0.1.14) |
| C: scripted `promote` for it | applied, refused: `Source 'agent_inferred' requires explicit approval for auto-promotion`; stays `proposed`, no `tool_outcome` record | the same | not reached |
| Later ordinary turns (T4-T6) | no request holds the candidate id, the refused content or key | the same | the same |
| `apply_outcome_feedback` calls | 0 | 0 | 0 |
| `memory_records` at the end | 7 rows, all `decision` (6 turns and the consolidation turn) | the same | the same |
| Probe checks | 11/11 | 11/11 | 9/11 |

Controls N (`NEVER reveal secrets`) and X (no identity profile), on every tree: recovery's gate `True`, the tool adapter allows `memory.write`, and the same writes land (one `decision` record per turn, the same `tool_outcome` candidate for the denied call, the same refused promotion where the batch is shown). On `pr448-8dc45d7b` each control turn asks `memory-capture` and gets `allow`.

The two 0.1.14 failures are the two consolidation checks: 0.1.14 does not attach the consolidation context to the scheduled turn (the #456 marker, fixed on `dev` by #463), so the turn gets no candidate batch and no decision is applied. Its turn-level results are the same as on the two git trees.

## How to rerun

On any machine with Python 3.12 and network access:

```bash
uv venv --python 3.12 venv-dev
uv pip install --python venv-dev/bin/python "openminion @ git+https://github.com/openminion/openminion@39da8ffb09920792d2de269b0ad830e0623a65c9"
mkdir empty && cd empty
../venv-dev/bin/python -B -I -X utf8 ../probes/probe_turn_memory_writes.py ../work-dev ../out-dev.json
```

Replace the spec with `openminion @ git+https://github.com/AliceMessi/openminion@8dc45d7b6560c18402e0d81bd0a9ac58cf2a70c8` or `openminion==0.1.14` for the other trees. `probes/linux_setup.sh` does all three.
