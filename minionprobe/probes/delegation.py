"""Delegation: bounded subagents and peer handoffs."""

from __future__ import annotations

from typing import Any

from ..core import ProbeContext, probe


@probe("delegation", "subagent(parent, ...) runs a bounded child with its own instructions")
def subagent_run(ctx: ProbeContext) -> str:
    from openminion import subagent

    marker = ctx.nonce("REVIEW")
    parent = ctx.agent(name="coordinator", instructions="You coordinate reviews.")
    child = subagent(
        parent,
        name="reviewer",
        instructions=f"Start your reply with {marker}: and then review the plan in one sentence.",
        timeout_seconds=150,
    )
    result, record = ctx.turn(
        child, "Plan: ship the release on Friday without running the test suite.", label="subagent", session_id=ctx.session("sub")
    )
    if result is None:
        ctx.check("subagent turn completed without exception", False, record.error)
        return record.error
    ctx.check("child followed its own instructions (marker present)", marker in result.text, f"text={result.text[:200]!r}")
    # documented-but-unbound parameter: memory_posture
    try:
        subagent(parent, name="reader", memory_posture="read_only_bounded")
        ctx.observe("subagent_memory_posture", accepted=True)
    except ValueError as exc:
        ctx.observe("subagent_memory_posture", accepted=False, error=str(exc))
    return f"marker_present={marker in result.text}"


@probe("delegation", "Handoff(target=...) transfers the turn to a peer agent")
def handoff(ctx: ProbeContext) -> str:
    from openminion import Handoff

    marker = ctx.nonce("RESEARCH")
    asked: list[dict[str, Any]] = []

    def approve(tool_name: str, args: dict[str, Any], approval_id: str) -> bool:
        asked.append({"tool": tool_name})
        return True

    researcher = ctx.agent(
        name="researcher",
        instructions=f"Begin your answer with {marker}: then answer in one sentence.",
    )
    coordinator = ctx.agent(
        name="coordinator",
        instructions="Delegate every research question to the researcher.",
        handoffs=[Handoff(target=researcher)],
        forced_tools=["transfer_to_researcher"],
    )
    result, record = ctx.turn(
        coordinator,
        "Research question: what does an HTTP 429 status code mean?",
        label="handoff",
        session_id=ctx.session("handoff"),
        approval_callback=approve,
    )
    handoff_called = any(
        "transfer_to_researcher" in e.tools for e in ctx.last_events
    )
    ctx.check("transfer_to_researcher was offered to the model", handoff_called, "")
    transfer_asks = [a for a in asked if "transfer_to" in a["tool"]]
    if transfer_asks:
        ctx.known_issue("#284")
        ctx.observe("handoff_needed_approval", asked=asked)
    if result is None:
        ctx.check("handoff turn completed without exception", False, record.error)
        return record.error
    ctx.check("peer agent produced the answer (marker present)", marker in result.text, f"text={result.text[:200]!r}")
    ctx.check("handoff did not require a risk confirmation", not transfer_asks, f"approvals={asked}")
    return f"marker_present={marker in result.text} approvals={asked}"
