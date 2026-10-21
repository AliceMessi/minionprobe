"""Models / providers: does the turn go where the configuration says?"""

from __future__ import annotations

from ..core import ProbeContext, ProbeSkipped, probe


@probe("models", "Requests reach the configured provider host and model", cost_turns=0)
def provider_routing(ctx: ProbeContext) -> str:
    record = ctx.shared.get("basic_turn")
    events = ctx.shared.get("basic_turn_events")
    if record is None or events is None:
        raise ProbeSkipped("needs agent_execution.basic_turn in the same run")
    hosts = sorted({e.host for e in events})
    models = sorted({str(e.model) for e in events})
    ctx.check("all LLM requests went to the configured host", hosts == [ctx.provider.host], f"hosts={hosts}")
    ctx.check("all LLM requests used the configured model", models == [ctx.provider.model], f"models={models}")
    ctx.check(
        "result metadata names the configured agent",
        (record.agent or "") == ctx.provider.agent_id,
        f"metadata.agent={record.agent} provider={record.provider} model={record.model}",
    )
    return f"hosts={hosts} models={models}"


@probe("models", "Explicit agent_id wins over an existing session's agent", scope="local", cost_turns=2)
def session_agent_binding(ctx: ProbeContext) -> str:
    """Two agents on two different models share one session id.

    The second ``Agent(agent_id="fake-b")`` turn should run on model-b. On
    0.1.8 it silently runs on the agent that created the session.
    """
    from ..fakeprovider import Fault

    fake = ctx.extras.get("fake")
    if fake is None:
        raise ProbeSkipped("needs the local fake provider")
    session = ctx.session("shared")
    # agent a's turn fails (provider 500), so there is no pending reply that
    # the follow-up-replay bug (sessions.followup_not_replayed) would replay
    fake.script.faults = [Fault(status=500, when="always", message="upstream unavailable")]
    try:
        ctx.turn(ctx.agent(agent_id="fake-a", tools=[]), "hello from agent a", label="agent-a (provider down)", session_id=session)
    finally:
        fake.script.faults = []
    mark = len(fake.requests)
    second = ctx.agent(agent_id="fake-b", tools=[])
    result, record = ctx.turn(second, "hello from agent b", label="agent-b", session_id=session)
    models = sorted({r["model"] for r in fake.requests[mark:]})
    ctx.check(
        "turn with agent_id=fake-b used model-b",
        models == ["model-b"],
        f"models requested={models} metadata.agent={record.agent}",
    )
    ctx.check("result metadata reports agent fake-b", record.agent == "fake-b", f"metadata.agent={record.agent}")
    # control: a fresh session for fake-b does use model-b
    mark = len(fake.requests)
    ctx.turn(second, "hello again", label="agent-b-fresh", session_id=ctx.session("fresh"))
    fresh_models = sorted({r["model"] for r in fake.requests[mark:]})
    ctx.check("control: fresh session for fake-b uses model-b", fresh_models == ["model-b"], f"models={fresh_models}")
    return f"shared-session turn hit {models}; fresh-session turn hit {fresh_models}"
