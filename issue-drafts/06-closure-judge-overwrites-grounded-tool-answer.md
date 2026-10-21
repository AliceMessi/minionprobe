<!-- DRAFT, NOT FILED. Review and re-run before filing. -->

# [0.1.8] Closure judge replaces a correct, tool-grounded answer with a refusal ("I do not have access to ...")

## Summary

A decorated `@tool` runs and its string result reaches the model. The model's agent-loop answer quotes it correctly. Then the hidden closure-judge call (`purpose=judge`, forced `submit_output`) returns a `final_answer` saying the agent cannot access the data, and that text becomes `result.text`. The user gets a refusal even though the tool succeeded.

## Environment

- openminion 0.1.8 (PyPI): 3 of 3 runs on 2026-10-21. `dev` at `b642697`: 2 of 2 runs.
- Provider: `gemini` preset, model `gemini-3.1-flash-lite` (Gemini API OpenAI-compatible endpoint, free tier)
- Windows 11 Pro 10.0.22631, Python 3.12.11

## Steps to reproduce

```python
from openminion import Agent, tool

@tool
def lookup_order(order_id: str) -> str:
    """Return the current status and pickup code for one order."""
    return f"order {order_id}: ready for pickup, pickup code ZEBRA-42"

with Agent(agent_id="gemini",
           instructions="Use lookup_order, then answer in one sentence that quotes the pickup code.",
           tools=[lookup_order], forced_tools=["lookup_order"]) as agent:
    r = agent.run("Check order A-104")
    print(r.text)
```

Or: `python -m minionprobe run --providers gemini --only decorated_tool`.

## Expected

`result.text` quotes the pickup code, e.g. "Order A-104 is ready for pickup, code ZEBRA-42."

## Actual

```
gemini: I am sorry, but I do not have access to an order management system to check the status of
order A-104. Please check with the customer support team of the company where you placed your order.
```

The HTTP trace of the turn (6 calls, redacted) shows what happened:

1. `submit_output` (memory reflect): 200
2. agent loop, tools `[lookup_order]`: the model calls `lookup_order`
3. `submit_output` (freshness classifier): the model returns `needs_live_data: true, needs_sources: true, answer_mode: "browse_then_answer"` for "Check order A-104"
4. agent loop with the tool result `{"status":"success","outputs":{"data":"order A-104: ready ... ZEBRA-42"}}`: the model answers **"Order A-104 is ready. The secret code for this order is ZEBRA-42."** with `<confident_complete>{"complete":true}`
5. `submit_output` (judge). The system prompt contains `action_summary: Order A-104 is ready. The secret code ... ZEBRA-42.` together with `freshness_obligations: {"require_live_data": true, "require_sources": true, "require_explicit_failure_wording": true}`. The model returns `final_answer: "I apologize, but I do not have access to an order management system ..."`
6. that `final_answer` becomes `result.text`

## Impact

- Silent loss of a correct, tool-grounded answer. `run_state=completed`, `tool_calls=1`, no error.
- It hits exactly the SDK's headline use case: an application tool (`lookup_order` is the quickstart example) answering a user question.
- It depends on the model. The same probe passes on Groq `openai/gpt-oss-120b` (see MinionProbe matrix), but nothing in the runtime protects the grounded answer.

## Possible directions

- Let a successful tool result count as the "live data/source" that satisfies the freshness obligations.
- Do not let the closure judge's `final_answer` replace an agent-loop answer that already completed (`confident_complete`), unless it adds information; or include the tool outputs explicitly in the judge context.

## Notes

Found by MinionProbe probe `tools.decorated_tool`. The probe flags `grounded_answer_replaced` when an agent-loop response contains the tool result but `result.text` does not.
