# OpenMinion: direct terminal capture and the identity hard constraint

The separate report requested when closing [openminion#420](https://github.com/openminion/openminion/issues/420#issuecomment-6441322163). In the 0.1.13 verification of #420 ([`openminion-0.1.13-verification`](../openminion-0.1.13-verification), case D), the identity hard constraint `NEVER write memory records` denied the cron capture recovery, while the turns' own terminal capture still reached the bundle write. That case used a one-shot injected store failure to leave a pending intent. This folder records the direct capture path with an unmodified, successful write and the constraint loaded.

## What ran

- Native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4, 27 Oct 2026.
- Two fresh `uv` venvs:
  - `openminion==0.1.13` from PyPI ([`windows/0.1.13`](windows/0.1.13));
  - `openminion @ git+https://github.com/openminion/openminion@68d623d10317a7cea13832f8dbd51fc98a871aa5`, current `dev` ([`windows/dev-68d623d1`](windows/dev-68d623d1); `direct_url.json` records the commit). Its version string is 0.1.13.
- [`probes/probe_capture_authorization.py`](probes/probe_capture_authorization.py), run with `python -I -X utf8` from an empty directory outside any checkout. No repo test fixtures.
- [`probes/probe_420_lib.py`](probes/probe_420_lib.py) and [`probes/_common.py`](probes/_common.py) are byte-identical copies of the helpers in [`openminion-0.1.13-verification/probes`](../openminion-0.1.13-verification/probes) (see [`windows/probe-sha256.txt`](windows/probe-sha256.txt)).
- Each case builds a real `APIRuntime` with one agent, `openminion`. Its `openai` provider points at a scripted OpenAI-compatible server on 127.0.0.1. The agent has an identity profile, `profile.yaml` under the configured `identity.bundle_root`. One SDK turn runs, "Please remember that I prefer tabs over spaces for indentation.", in session `capture`. The scripted auto fact extraction returns one `user_preference` candidate.
- Nothing is injected. The probe wraps runtime objects with recorders that call the original and never change a result: the memctl bundle write, the capture extractor, and every `SecurityPolicyEngine.evaluate` call.
- After the turn, the probe asks two gates about the same capture:
  - the recovery gate: the installed `memory_capture_recovery_allowed`, with the arguments the cron executor passes (security policy, tools, identity agent id);
  - the tool gate: the turn's own bound tool-policy adapter (`runner.tool_api.policy_adapter.evaluate`) for the `memory.write` tool.

## Results

0.1.13 and `dev` give the same results: 9/9 checks pass on each.

| | W: `NEVER write memory records` | C (control): `NEVER reveal secrets` |
|---|---|---|
| Constraint loaded (profile file, `IdentityCtl` profile, `SecurityPolicyEngine` identity constraints) | yes | yes |
| Turn | `completed` | `completed` |
| Provider requests during the turn | extraction, decide, freshness, freshness | same |
| Direct terminal capture | one bundle write with the candidate; `memory_capture_state: succeeded`; disposition `succeeded`, 1 output id | same |
| Memory store afterwards | 1 `memory_candidates` row (status `proposed`, the turn's `capture_id`), 1 receipt row `succeeded`, 1 `memory.capture.result` | same |
| Security-policy evaluations during the turn | 2: `gateway turn.execute`, `channel message.send`, both `allow` | same |
| Recovery gate, same capture (`memory.write`, channel `cron`, target `memory-capture-recovery`) | `False`: `require_approval`, `identity_hard_constraint_restricted` | `True`: `allow` |
| Tool gate, `memory.write` (channel `console`, target `tool-runtime`) | not allowed: `require_approval`, `identity_hard_constraint_restricted` | allowed |

## Files

- `windows/<build>/probe_capture_authorization.json`: per case, the identity facts (including the profile text), the turn result, the bundle write, every policy evaluation, the provider requests, the memory rows and both gate results.
- `windows/<build>/probe_capture_authorization.log`: the console output.
- `windows/<build>/pip-list.txt`, `pip-show.txt` (and `direct_url.json` for `dev`).
- Paths are sanitized (`<VENV>`, `<WORK>`).

## Limits

- Both gate results come from the probe calling the package functions after the turn. The scripted provider does not issue a `memory.write` tool call.
- Native Windows with the built-in SQLite memory store only. Not run on Linux or PostgreSQL.
- No fix and no code change.
