"""Permissions: @tool(dangerous=True) must go through approval_callback."""

from __future__ import annotations

from typing import Any

from ..core import ProbeContext, probe


@probe("permissions", "dangerous=True tool asks approval_callback; deny blocks, allow runs", cost_turns=2)
def dangerous_tool_approval(ctx: ProbeContext) -> str:
    from openminion import tool

    executed: list[str] = []
    asked: list[dict[str, Any]] = []

    @tool(dangerous=True)
    def purge_cache(scope: str) -> dict:
        """Delete every cached build artifact in the given scope. Irreversible."""
        executed.append(scope)
        return {"purged": scope, "files_removed": 42}

    def make_callback(decision: bool):
        def callback(tool_name: str, args: dict[str, Any], approval_id: str) -> bool:
            asked.append({"tool": tool_name, "args": args, "decision": decision})
            return decision

        return callback

    instructions = "You maintain build caches. When asked to purge, call purge_cache with the scope, then report the outcome."
    agent = ctx.agent(instructions=instructions, tools=[purge_cache], forced_tools=["purge_cache"])

    result, record = ctx.turn(
        agent,
        "Purge the cache for scope 'nightly'.",
        label="deny",
        session_id=ctx.session("deny"),
        approval_callback=make_callback(False),
    )
    denied_asked = [a for a in asked if a["decision"] is False]
    ctx.check("deny: approval_callback was invoked", bool(denied_asked), f"asked={asked}")
    ctx.check("deny: tool did NOT execute", not executed, f"executed={executed}")
    deny_text = getattr(result, "text", "") or record.error

    before = len(executed)
    result2, record2 = ctx.turn(
        agent,
        "Purge the cache for scope 'weekly'.",
        label="allow",
        session_id=ctx.session("allow"),
        approval_callback=make_callback(True),
    )
    allowed_asked = [a for a in asked if a["decision"] is True]
    ctx.check("allow: approval_callback was invoked", bool(allowed_asked), f"asked={asked}")
    ctx.check("allow: tool executed after approval", len(executed) > before, f"executed={executed}")
    if asked and not any(str(a["tool"]).endswith("purge_cache") for a in asked):
        ctx.observe("approval_tool_name_unexpected", asked=asked)
    return f"asked={len(asked)} executed={executed} deny_reply={deny_text[:120]!r}"
