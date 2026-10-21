<!-- Filed upstream on 2026-10-21 as https://github.com/openminion/openminion/issues/326 -->

# [0.1.8] Rate limit (429) during a turn is still returned as reply text with `run_state=completed` (#251 fix incomplete)

## Summary

#251 was fixed in 0.1.5: a provider failure *before* the model answers now raises `ProviderError`. On 0.1.8 a 429 on a later call in the same turn still comes back as a normal result: `result.text == "<agent>: RATE_LIMITED: ..."`, `run_state == "completed"`, and no exception. The later call can be the one right after a tool has run, or the follow-up agent-loop call.

## Environment

- openminion 0.1.8 (PyPI)
- Windows 11 Pro 10.0.22631, Python 3.12.11
- Seen on a local OpenAI-compatible fake server (deterministic), on Groq (`openai/gpt-oss-120b`, free tier) and on Gemini (`gemini-3.1-flash-lite`, free tier)

## Steps to reproduce (deterministic, offline)

MinionProbe's fake provider returns HTTP 429 only for the agent-loop call that already carries a tool result:

```bash
python -m minionprobe run --providers local --only rate_limit_after_tool
```

Outline of the probe:

```python
@tool
def lookup_order(order_id: str) -> str:
    """Return the status of one order."""
    return f"order {order_id}: ready"

agent = Agent(runtime=rt, agent_id="local", tools=[lookup_order], forced_tools=["lookup_order"])
result = agent.run("Check order A-104")      # fake server: 200 tool call, then 429 after the tool result
print(result.run_state, result.text)
```

## Expected

`ProviderError(code="RATE_LIMITED")` is raised, as it is when the first call is rate limited. That is the #251 contract and the `examples/sdk/error_handling.py` pattern.

## Actual

```
run_state=completed text='local: RATE_LIMITED: openai rate limited: Rate limit reached for model fake-model. Please try again in 1s.'
```

Real providers, 2026-10-21 (organization id redacted):

```
groq   run_state=completed text='groq: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-120b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 7493, Requested 1560. Please try again in 7.89s. ...'
gemini run_state=completed text='gemini: RATE_LIMITED: openai rate limited: HTTP 429'
```

The Groq case happened after the decorated tool had run. In the same session, a 429 on the *first* call correctly raised `ProviderError RATE_LIMITED`.

## Impact

- Applications that rely on `except ProviderError` for retry and backoff (the documented pattern) show the raw rate-limit message to end users as if it were the model's answer.
- The provider's error message is copied into the reply body. Groq's includes the account's organization id, so it can end up in chat transcripts, logs and memory capture.
- `run_state=completed` makes the failure invisible to monitoring.

## Notes

- Related: #251 (closed). This looks like the same contract on a different code path (tool-loop continuation / post-tool synthesis).
- MinionProbe flags every reply matching `^<agent>: <CODE>: ` as `provider_error_returned_as_text` in its results.
