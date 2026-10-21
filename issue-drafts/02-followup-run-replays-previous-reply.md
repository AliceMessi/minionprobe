<!-- DRAFT, NOT FILED. Review and re-run before filing. -->

# [0.1.4–0.1.8] A follow-up `Agent.run` on the same `session_id` replays the first reply instead of running a new turn

## Summary

After the first successful `Agent.run` in a session, every later `Agent.run` with the same `session_id` gets the first answer back. The new message is never sent to the model: zero provider requests, the same `run_id`, and `metadata.thread_decision_action == "replay"` with reason `undelivered_response_pending`. The documented `examples/sdk/sessions.py` is affected.

## Environment

- openminion 0.1.8 (PyPI), 0.1.4 (PyPI) and `dev` at `b642697` (2026-10-20): same result on all three
- Windows 11 Pro 10.0.22631, Python 3.12.11
- Providers: built-in `echo` (offline), a local OpenAI-compatible fake server, and Gemini (`gemini-3.1-flash-lite`)

## Steps to reproduce (offline, echo provider)

```bash
openminion --home-root ./proj --data-root ./proj/.openminion config init --provider echo
```

```python
import logging
from pathlib import Path
logging.disable(logging.CRITICAL)
from openminion import Agent, APIRuntime

proj = Path("proj").resolve()
rt = APIRuntime.from_config_path(str(proj / ".openminion" / "agents.json"),
                                 home_root=proj, data_root=proj / ".openminion")
with Agent(runtime=rt, session_id="replay-demo") as agent:
    for msg in ("first message", "second message", "third message"):
        r = agent.run(msg)
        print(f"{msg!r:18} run_id={r.run_id[:8]} thread={r.metadata.get('thread_decision_action')} text={r.text!r}")
rt.close()
```

## Expected

Three turns, three different replies (the echo provider echoes each message), and three run ids.

## Actual

```
'first message'    run_id=88388642 thread=fork_thread text='openminion: first message (thinking=minimal)'
'second message'   run_id=88388642 thread=replay      text='openminion: first message (thinking=minimal)'
'third message'    run_id=88388642 thread=replay      text='openminion: first message (thinking=minimal)'
```

The unmodified `examples/sdk/sessions.py` behaves the same against an OpenAI-compatible endpoint. "shared second" prints the first answer, and the provider receives no request for that turn:

```
shared first:  official-demo: fake: FAKE-ANSWER(fake-model): Remember that the launch color is teal.
shared second: official-demo: fake: FAKE-ANSWER(fake-model): Remember that the launch color is teal.
isolated:      sdk-isolated-session: fake: FAKE-ANSWER(fake-model): What is the launch color?
```

On Gemini (real model), MinionProbe's `sessions.continuity_isolation` recorded the same behaviour. The follow-up "What is the launch code word?" made 0 HTTP requests and returned the previous reply `noted.`.

## Analysis (from reading 0.1.8)

- `Agent._build_payload` sends `"deliver": False` and no `caller_handles_delivery` inbound metadata.
- `services/gateway/turn/flow_persistence.py` emits `response.delivered` only when `deliver or caller_handles_delivery`. For SDK turns it is never emitted.
- `modules/task/run/status.py::_resolve_thread_state` then reports `response_ready_undelivered` for the session. `resolve_thread_routing_decision` returns `THREAD_DECISION_REPLAY` (`undelivered_response_pending`) for every later message, so the pending first reply is replayed forever.
- The CLI paths (`cli/commands/agent/runner.py`, `cli/interactive/runtime/messages.py`) set `CALLER_HANDLES_DELIVERY_METADATA_KEY: "true"`. The SDK path does not.

## Impact

- Multi-turn conversations through the public SDK are broken: same-session continuity, the main reason to pass `session_id`, returns stale answers.
- Failure is silent: `run_state == "completed"`, no exception, and a plausible-looking reply.
- Cost and telemetry look fine (no calls are made), which hides the problem in dashboards.

## Workaround

Use a new `session_id` for every turn, which gives up continuity. A failed turn (provider error) also "settles" the thread, but that is not a workaround.

## Notes

- Found by MinionProbe: `sessions.followup_not_replayed` (deterministic, local fake provider) and `sessions.continuity_isolation` (real providers).
- Not covered by #248/#249/#251/#257–#262/#269/#270/#284.
