"""Memory: a durable fact captured in one session is recalled in another."""

from __future__ import annotations

from ..core import ProbeContext, probe


@probe("memory", "Durable fact captured in session A is recalled in session B", cost_turns=2)
def cross_session_recall(ctx: ProbeContext) -> str:
    tool_name = ctx.nonce("buildy").lower()
    agent = ctx.agent(instructions="You are a helpful assistant with long-term memory. Keep answers short.")
    result, record = ctx.turn(
        agent,
        f"Please remember this about me permanently: my favourite build tool is called {tool_name}.",
        label="capture",
        session_id=ctx.session("mem-a"),
    )
    control_errors = [
        {"status": e.status, "error": e.error[:200]} for e in ctx.last_events if e.kind == "control" and e.status >= 400
    ]
    ctx.observe(
        "memory_capture",
        state=record.memory_capture_state,
        control_call_errors=control_errors[:3],
        purposes=record.purposes,
    )
    ctx.check(
        "memory capture did not fail or get rejected",
        str(record.memory_capture_state or "").lower() not in {"failed_terminal", "rejected"},
        f"memory_capture_state={record.memory_capture_state} control_errors={len(control_errors)}",
    )
    recall, record2 = ctx.turn(
        agent,
        "What is my favourite build tool called? Reply with the name only, or UNKNOWN.",
        label="recall",
        session_id=ctx.session("mem-b"),
    )
    ok = recall is not None and tool_name in recall.text.lower()
    ctx.check("new session recalls the fact", ok, f"text={getattr(recall, 'text', record2.error)[:160]!r}")
    return f"capture_state={record.memory_capture_state} recalled={ok}"
