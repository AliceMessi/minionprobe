# OpenMinion: a tool call the user denies leaves its arguments in a `tool_outcome` candidate

Evidence for the report split out of [openminion#496](https://github.com/openminion/openminion/issues/496) at the maintainer's request: under an otherwise allowed memory policy, a `memory.write` call that the user denies through the SDK approval callback still stages a `tool_outcome` candidate whose `content.args_signature` and `meta.source_args_signature` are the denied arguments. Current `dev`, the #471 head and the #496 PR head.

## Trees

| Name | Installed from | What it is |
|---|---|---|
| `dev-ce3b6771` | `openminion/openminion@ce3b6771` | current `dev` ("Merge pull request #470") |
| `pr471-6064d6d7` | `AliceMessi/openminion@6064d6d7` | the #471 head (`fix/448-capture-authorization`) |
| `pr496-44a6e239` | `AliceMessi/openminion@44a6e239` | the #496 PR head (`fix/496-optional-memory-writes-policy`, stacked on #471) |

All three report `__version__` 0.1.15; `direct_url.json` in each JSON banner records the installed commit.

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes.

- `probes/probe_user_denied_tool_args.py`: the probe. `probes/probe_420_lib.py` and `probes/_common.py` are the #448 probe's helpers, unchanged (same sha256 as in [`openminion-448`](../openminion-448) and [`openminion-turn-memory-writes-under-constraint`](../openminion-turn-memory-writes-under-constraint); `probe_420_lib.py` keeps its CRLF line endings). Installed package only, no repo test fixtures; it runs with `python -I -X utf8` from an empty directory. 9 checks.
- `probes/linux_setup.sh`: the runner script. It installs the three trees into fresh venvs (`uv venv --python 3.12`) and runs two probes on each: this one and the #496 report's `probe_turn_memory_writes.py` (unchanged; its outputs are in [`openminion-496`](../openminion-496), for the PR).
- `linux/`: [run 40819905175](https://github.com/AliceMessi/linux-runner/actions/runs/40819905175) (`run-id.txt`): GitHub-hosted Ubuntu 24.04, CPython 3.12.15, SQLite 3.45.1. The runner checked out `AliceMessi/openminion@0956b52a`, which is `dev` @ `ce3b6771` plus a `probe_tmp/` folder with the five probe files and nothing else (`head.txt`), and ran `PR496=44a6e239f18b00e84a3e49f4fbc6ccca4f83578b bash probe_tmp/linux_setup.sh`. `probes.sha256` shows the probe bytes that ran (the same as here). Per tree: `probe_user_denied_tool_args_<tree>.json` and `.log`, `spec_<tree>.txt`, `pip-list_<tree>.txt`; `run.log` is the job's output for both probes.

Paths are shortened to `<VENV>` and `<RUNNER_TEMP>`.

## What the probe does

A real `APIRuntime` with the built-in SQLite memory store and one agent, `openminion`, whose `openai` provider points at a scripted OpenAI-compatible server on 127.0.0.1. Three cases, each a fresh runtime home: **N** identity profile with `NEVER reveal secrets`; **X** no identity profile; **W** `NEVER write memory records` (reference for #496, no checks).

In each case, three SDK turns in their own sessions, `Agent(forced_tools=["memory.write"]).run("Which indentation style do I prefer?")`. The scripted model calls `memory_write` once with `{"record_type": "fact", "key": "fact:indentation", "title": "Indentation", "content": "The user prefers tabs."}`:

| Step | Approval callback |
|---|---|
| D | records the call and denies |
| A | records the call and approves |
| P | none |

Then it calls `collect_memory_consolidation_candidates(memctl, proposed_scope="agent:openminion", limit=8)`, the collector the consolidation turn's context uses. Call-through recorders (they return the original result) keep every `stage_candidate` call of the brain's memory API with its caller frames and every `memory.write` decision of the agent's security policy. The probe reads the `tool_outcome` rows of `memory_candidates` and the `fact:indentation` rows of `memory_records` before and after each step.

## Results

N and X, on every tree:

| | `dev-ce3b6771` | `pr471-6064d6d7` | `pr496-44a6e239` |
|---|---|---|---|
| D: approval callback | once, `memory.write` with the arguments above, denied; reply "Confirmation denied. The action was not run." | the same | the same |
| D: `fact:indentation` record | none | none | none |
| D: `tool_outcome` candidate | 1: `tool_outcome:memory.write:policy_denied`, `agent:openminion`, `proposed`, source `agent_inferred`; `content.args_signature` and `meta.source_args_signature` = `{"content":"The user prefers tabs.","key":"fact:indentation","record_type":"fact","title":"Indentation"}`; staged from `command_executor.py:209 prepare_tool_dispatch` through `_stage_tool_outcome_candidate` | the same | the same |
| D: `memory.write` decisions | none | `memory-capture`: `allow` | `memory-capture`: `allow` |
| A: callback, record, candidate | once, approved; 1 record; none (`tool-runtime`: `allow`) | the same | the same |
| P: reply, record, candidate | "Policy confirmation required." with the choices; no record; 1 candidate like D's | the same | the same |
| Consolidation collector | lists the D and P candidates; each preview ends `'args_signature': '{"content":"The user prefers` | the same | the same |
| Probe checks | 9/9 | 9/9 | 9/9 |

W (reference): D and P stage the same candidate on `dev-ce3b6771` and `pr471-6064d6d7`, and none on `pr496-44a6e239` (the turn's `memory-capture` answer is `require_approval`). A writes the record on all three.

## How to rerun

On any machine with Python 3.12 and network access:

```bash
uv venv --python 3.12 venv-dev
uv pip install --python venv-dev/bin/python "openminion @ git+https://github.com/openminion/openminion@ce3b677173e0f344d85a729113e04cf708fa6587"
mkdir empty && cd empty
../venv-dev/bin/python -B -I -X utf8 ../probes/probe_user_denied_tool_args.py ../work-dev ../out-dev.json
```

Replace the spec with `openminion @ git+https://github.com/AliceMessi/openminion@<commit>` for the other trees. `probes/linux_setup.sh` does all three (it also needs `probe_turn_memory_writes.py` next to it, from [`openminion-496/probes`](../openminion-496/probes)).
