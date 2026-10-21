<!-- DRAFT, NOT FILED. Review and re-run before filing. -->

# [0.1.8] `Agent(agent_id="b")` silently runs on agent "a"'s provider when the session was first used by "a"

## Summary

A `session_id` first used by agent `a` stays bound to `a`. A later `Agent(agent_id="b").run(..., session_id=same)` does not run on `b`: the request goes to `a`'s provider and model, and `result.metadata["agent"]` says `a`. There is no error or warning. With real providers this sends the prompt to a provider the caller did not select.

## Environment

- openminion 0.1.8 (PyPI) and `dev` at `b642697`
- Windows 11 Pro 10.0.22631, Python 3.12.11
- Two agent profiles created with `openminion setup --agent ... --provider custom-openai-compatible` pointing at a local OpenAI-compatible fake server (`model-a`, `model-b`). Also observed with real `groq` and `gemini` profiles.

## Steps to reproduce

MinionProbe ships the fake server and the probe:

```bash
python -m minionprobe run --providers local --only session_agent_binding
```

Standalone outline (any two configured agents `a` and `b` with different models):

```python
from openminion import Agent, APIRuntime, ProviderError
rt = APIRuntime.from_config_path(cfg, home_root=home, data_root=home / ".openminion")
S = "shared-session"
# 1. agent a's turn fails (provider down / 5xx), so there is no pending reply to replay (see draft 02)
try:
    Agent(runtime=rt, agent_id="a").run("hello from a", session_id=S)
except ProviderError:
    pass
# 2. explicit agent b on the same session
r = Agent(runtime=rt, agent_id="b").run("hello from b", session_id=S)
print(r.metadata["agent"], r.text)
```

## Expected

The second turn runs on agent `b` (`model-b`), or the SDK raises a clear error saying the session belongs to another agent.

## Actual

```
fake-a  'first (provider down)' -> models=['model-a'] ProviderError PROVIDER_ERROR
fake-b  'second from b'         -> models=['model-a'] agent=fake-a state=completed text='fake-a: FAKE-ANSWER(model-a): second from b' thread=resume_thread
```

Control: the same `Agent(agent_id="fake-b")` with a fresh session uses `model-b`.

Real-provider observation (2026-10-21): a session first used by the `groq` agent was then used with `Agent(agent_id="gemini")`. All HTTP requests went to `api.groq.com` with model `openai/gpt-oss-120b`, and the reply was prefixed `groq:`.

## Impact

- Prompts and data go to a provider and model the caller did not choose. That matters for cost, data residency and provider policy.
- Provider-switching code (`examples/sdk/provider_switching.py` pattern) silently keeps using the old provider if the application reuses session ids.

## Notes

- Found by MinionProbe probe `models.session_agent_binding` (local fake provider, offline, deterministic).
- Separate from draft 02 (replay): here the model is actually called, just the wrong one.
