"""Tools: a decorated @tool is offered, executed, and its result is used."""

from __future__ import annotations

from ..core import ProbeContext, probe, has


@probe("tools", "@tool result reaches the model and the final answer")
def decorated_tool(ctx: ProbeContext) -> str:
    from openminion import tool

    code = ctx.nonce("ZEBRA")
    calls: list[str] = []

    @tool
    def lookup_order(order_id: str) -> str:
        """Return the current status and pickup code for one order."""
        calls.append(order_id)
        return f"order {order_id}: ready for pickup, pickup code {code}"

    agent = ctx.agent(
        instructions="Use lookup_order, then answer in one sentence that quotes the pickup code.",
        tools=[lookup_order],
        forced_tools=["lookup_order"],
    )
    result, record = ctx.turn(agent, "Check order A-104", label="tool", session_id=ctx.session("tool"))
    events = ctx.last_events
    offered = any("lookup_order" in e.tools for e in events)
    reached = any(
        e.has_tool_result and any(code in str(m.get("content", "")) for m in (e._messages or []) if m.get("role") == "tool")
        for e in events
    )
    model_used_it = any(e.kind == "agent_loop" and code in e._response_text for e in events)
    ctx.check("tool was offered to the model", offered, f"tools offered={sorted({t for e in events for t in e.tools})}")
    ctx.check("python tool function executed", bool(calls), f"calls={calls}")
    ctx.check("tool result (str) reached the model", reached, "looked for the pickup code in role=tool messages")
    if result is None:
        ctx.check("turn completed without exception", False, record.error)
        return record.error
    final_ok = has(result.text, code)
    ctx.check("final answer quotes the tool result", final_ok, f"text={result.text[:240]!r}")
    if model_used_it and not final_ok:
        ctx.observe(
            "grounded_answer_replaced",
            detail="the agent-loop response contained the tool result, but result.text does not",
            final_text=result.text[:300],
            purposes=record.purposes,
        )
    return f"calls={calls} model_used_result={model_used_it} final_ok={final_ok}"
