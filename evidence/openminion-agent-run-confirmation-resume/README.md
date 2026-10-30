# OpenMinion: a second `Agent.run` on the same session does not answer a waiting confirmation

Evidence for a focused report requested in the #534 review: the normal SDK follow-up, a second `Agent.run("yes")` on the same `Agent`, does not resume a tool call that is waiting for the user's confirmation. It is routed to a new thread (`fork_thread`, `settled_without_resume`), and the call does not run. The same "yes" sent on the first turn's thread (explicit-thread control) resumes it and the call runs once. Installed `openminion==0.1.16` from PyPI only. This is not #527 (cancelling a turn while it waits for approval): nothing is cancelled here.

First seen in the #534 exploration on the dev tree ([`openminion-524`](../openminion-524), explore4, [run 40860841929](https://github.com/AliceMessi/linux-runner/actions/runs/40860841929), case `yes-agent-resume`); this probe pins it on the published package with the explicit-thread control.

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes. Local paths are printed as `<VENV>`, `<WORK>`, `<RUNNER_TEMP>`; runner paths are kept.

- `probes/probe_agent_run_confirmation_resume.py`: the probe (below).
- `probes/probe_420_lib.py`, `probes/_common.py`: the #448/#496 probe helpers, byte-identical to the copies in [`openminion-496/probes`](../openminion-496/probes) (`probe_420_lib.py` keeps its CRLF line endings so that its sha256 stays the same). They give a real `APIRuntime` for one agent, `openminion`, whose `openai` provider is a scripted OpenAI-compatible server on 127.0.0.1 (dummy key `local-dummy-key`, no other network).
- `probes/linux_setup.sh`: Linux runner script (fresh venv, `pip install openminion==0.1.16`, the probe three times).
- `probes/run_windows.sh`: native Windows script (Git Bash; fresh `uv` venv with CPython 3.12, `openminion==0.1.16`, the probe twice).
- `linux/`: [run 41027018327](https://github.com/AliceMessi/linux-runner/actions/runs/41027018327) (`run-id.txt`, `run.log`, `result.txt`). The runner checked out `AliceMessi/openminion@681ae2b6` (`head.txt`), which is `dev` @ `f7b6100a` plus a `probe_tmp/` folder with the four probe files and nothing else; the probe imports only the PyPI package, from an empty directory in isolated mode. `probe_run{1,2,3}.{json,log}`, `pip-freeze.txt`, `pip-install.log`, `probes.sha256` (the bytes that ran).
- `windows/`: Windows 11 (10.0.22631), CPython 3.12.11, SQLite 3.50.4. `probe_run{1,2}.{json,log}`, `pip-freeze.txt`, `probes.sha256`. The JSON and log files were converted from CRLF to LF after the run; nothing else was changed.

## The probe

`python -I -X utf8 probe_agent_run_confirmation_resume.py <work_dir> <out.json> --expect-version 0.1.16`, from an empty directory. Two paths, each with its own fresh runtime home and one `Agent(runtime=runtime, forced_tools=["memory.write"])`, without an `approval_callback`:

- Call 1 (both paths): `agent.run("Please remember that I prefer tabs over spaces for indentation.")`. The scripted model calls `memory_write` once with `{"record_type": "fact", "key": "fact:editor_font", "title": "Editor font", "content": "The user writes code in Iosevka 14."}`. With no callback the call waits for the user's confirmation.
- **(a) normal two-call path.** Call 2: `agent.run("yes")` on the same `Agent` (so the same `session_id`; `Agent.run` takes no thread). Call 3, an observation after the two calls: "yes" on call 1's thread, as in (b).
- **(b) explicit-thread control.** Call 2: `runtime.run_turn(payload=...)`, where the payload is the one `Agent.run` builds for "yes" (`Agent._build_payload("yes")`) plus call 1's `thread_id` in `inbound_metadata`.

Recorded per call: session id, thread id, `thread_decision_action`/`thread_decision_reason`, run id and `run_state`, `respond_kind`, `pending_confirmation_command_id`, the reply, the scripted model requests, every call of the `memory.write` handler (call-through recorder), and the `fact:editor_font` record. Per path: the session's `thread.decision`, `run.*`, `policy_confirmation_prompt` and `response.*` events with their thread and run ids.

The scripted model's replies are fixed text. "Consolidation reviewed; nothing to change." is the helpers' default text reply, not a consolidation run.

## Results

Every run gave the same pattern; the probe's 7 checks passed in each (3 Linux runs, 2 Windows runs). Linux run 1 (`linux/probe_run1.json`):

| | session | thread | thread decision | run (state) | `respond_kind` | pending confirmation | handler calls | record |
|---|---|---|---|---|---|---|---|---|
| (a) call 1, `agent.run(<capture message>)` | `3f4dca7fb7f6400284e3ee8d952b49ea` | `…:ed7db3e5dbbe40b28a99ead4fcf3a9eb` | `fork_thread` / `settled_without_resume` | `ab31cb60ec094183a8279afbd1999bb1` (completed) | `policy_confirmation_prompt` | `call-memory-write-1` | 0 | none |
| (a) call 2, `agent.run("yes")` | same | `…:07a4747dde7b4b29a63c2ab067af9167` (new) | `fork_thread` / `settled_without_resume` | `a54b9282e43f407a849ea3ad9628b207` (completed) | `assistant` | none | 0 | none |
| (a) call 3, "yes" on call 1's thread | same | `…:ed7db3e5dbbe40b28a99ead4fcf3a9eb` | `resume_thread` / `explicit_thread_requested` | `cd4e738df5e747b2b09f9e530b927741` (completed, passed `waiting_tool`) | `assistant` | none | 1 | written |
| (b) call 1, `agent.run(<capture message>)` | `d31e155fca6e4c93ae94389082a02b14` | `…:738d4b93e80646a1968d5411624aff1c` | `fork_thread` / `settled_without_resume` | `6f66e86cda734c4ead4ec10b6ccb98d1` (completed) | `policy_confirmation_prompt` | `call-memory-write-1` | 0 | none |
| (b) call 2, "yes" with call 1's `thread_id` | same | `…:738d4b93e80646a1968d5411624aff1c` (same) | `resume_thread` / `explicit_thread_requested` | `0f24b944790f4cccb2cdf720669e895c` (completed, passed `waiting_tool`) | `assistant` | none | 1, the model's four arguments | written |

Thread ids are `<session id>:<hex>`; the table shows the part after the colon.

- (a) call 2: the model got a new `decide` request and answered with text; no tool call, no confirmation replay. The reply was "openminion: Consolidation reviewed; nothing to change." (the scripted default).
- (a) call 3 and (b) call 2: the waiting call ran once with the model's arguments, `fact:editor_font` was written, and the reply was "openminion: Noted." (one `judge` request).
- Call 1 in both paths is itself `fork_thread` / `settled_without_resume`: the first turn of a new session.

Session events of path (a), Linux run 1 (`paths.a.session_events` in the JSON): `thread.decision` → `run.queued` → `run.running` → `run.responding` → `policy_confirmation_prompt` → `response.persisted` → `response.delivered` (`delivery_mode` `return`) → `run.completed` on thread `…ed7db3e5`; then the same sequence, without `policy_confirmation_prompt`, on the new thread `…07a4747d` for call 2; then on `…ed7db3e5` again for call 3 with `run.waiting_tool` before `run.responding`.

## Rerun

Linux (any fresh venv): `python -m venv v && v/bin/pip install openminion==0.1.16`, then from an empty directory `v/bin/python -I -X utf8 <probes>/probe_agent_run_confirmation_resume.py <work_dir> <out.json> --expect-version 0.1.16`; or the runner script `bash probe_tmp/linux_setup.sh` from a checkout that has the four files in `probe_tmp/`. Windows: `bash run_windows.sh <work_dir> <probes_dir> <out_dir>` in Git Bash with `uv`.

## Not covered

- Only the SDK `Agent` facade and `APIRuntime.run_turn` were run; the CLI, the gateway channels and the HTTP API were not.
- The `resume` inbound-metadata flag was not run; only the explicit `thread_id`.
- Only `memory.write` was used for the waiting call; other tools that ask for confirmation go through the same approval path (code reading).
- An approval callback answers inside the same turn and was not the subject here.
- SQLite runtime only; PostgreSQL was not run.
- No change to openminion is proposed or made here.
