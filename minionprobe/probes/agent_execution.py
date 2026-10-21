"""Agent execution: one plain turn and one typed (structured output) turn."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..core import ProbeContext, probe, has

STATS_KEYS = ("input_tokens", "output_tokens", "llm_calls", "tool_calls", "duration_ms")


@probe("agent_execution", "Plain Agent.run turn follows an exact instruction")
def basic_turn(ctx: ProbeContext) -> str:
    token = ctx.nonce("PONG")
    agent = ctx.agent(instructions=f"You are a test probe. Reply with exactly {token} and nothing else.")
    result, record = ctx.turn(agent, "ping", label="basic", session_id=ctx.session("basic"))
    ctx.shared["basic_turn"] = record
    ctx.shared["basic_turn_events"] = list(ctx.last_events)
    if result is None:
        ctx.check("turn completed without exception", False, record.error)
        return record.error
    ctx.check("run_state is completed", result.run_state == "completed", f"run_state={result.run_state}")
    ctx.check("reply contains the exact token", has(result.text, token), f"text={result.text[:200]!r}")
    missing = [k for k in STATS_KEYS if k not in result.stats]
    ctx.check("result.stats has the documented keys", not missing, f"missing={missing}")
    hidden = record.http.get("by_kind", {}).get("control", 0)
    ctx.observe(
        "hidden_llm_calls",
        http_requests=record.http.get("requests"),
        control_calls=hidden,
        stats_llm_calls=result.stats.get("llm_calls"),
        purposes=record.purposes,
    )
    return f"{record.http.get('requests')} HTTP calls for one turn ({hidden} hidden control calls)"


class IssueLabel(BaseModel):
    label: Literal["bug", "feature", "question"]
    confidence: float = Field(ge=0, le=1)


@probe("agent_execution", "Structured output with output_type=PydanticModel")
def structured_output(ctx: ProbeContext) -> str:
    from openminion import AgentOutputValidationError

    agent = ctx.agent(
        instructions="Classify GitHub issue titles. Return only a JSON object with keys label and confidence.",
        output_type=IssueLabel,
    )
    try:
        result, record = ctx.turn(
            agent,
            "Issue title: 'App crashes with a stack trace when saving a file whose name contains an emoji'",
            label="structured",
            session_id=ctx.session("structured"),
        )
    except AgentOutputValidationError as exc:  # raised by Agent._coerce_output
        ctx.check("output validates against output_type", False, f"raw={exc.raw_text[:200]!r}")
        return "AgentOutputValidationError"
    if result is None:
        ctx.check("turn completed without exception", False, record.error)
        return record.error
    ok = isinstance(result.output, IssueLabel)
    ctx.check("result.output is an IssueLabel instance", ok, f"type={type(result.output).__name__}")
    if ok:
        ctx.check("label is 'bug'", result.output.label == "bug", f"output={result.output!r}")
    return f"output={result.output!r}"[:200]
