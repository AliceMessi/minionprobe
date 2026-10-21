"""Sessions: same session_id continues the conversation, a new one is isolated."""

from __future__ import annotations

from ..core import ProbeContext, ProbeSkipped, probe, has


@probe("sessions", "Same session_id remembers; a different session_id is isolated", cost_turns=3)
def continuity_isolation(ctx: ProbeContext) -> str:
    word = ctx.nonce("TEAL")
    agent = ctx.agent(instructions="You are a concise assistant. Keep answers to one short line.")
    shared = ctx.session("shared")
    ctx.turn(agent, f"For this conversation only, the launch code word is {word}. Reply only: noted.", label="set", session_id=shared)
    result, record = ctx.turn(
        agent, "What is the launch code word? Reply with the word only.", label="recall-same", session_id=shared
    )
    same_ok = result is not None and has(result.text, word)
    ctx.check("same session recalls the word", same_ok, f"text={getattr(result, 'text', record.error)[:160]!r}")
    isolated, record3 = ctx.turn(
        agent,
        "What is the launch code word? If you do not know, reply UNKNOWN.",
        label="recall-isolated",
        session_id=ctx.session("isolated"),
    )
    leaked = isolated is not None and has(isolated.text, word)
    ctx.check(
        "different session does not see the word",
        not leaked,
        f"text={getattr(isolated, 'text', record3.error)[:160]!r}"
        + (" (if this fails, check whether durable memory carried it across sessions)" if leaked else ""),
    )
    if record.http.get("requests") == 0 and result is not None:
        ctx.observe("followup_replayed", label="recall-same", text=result.text[:200])
    return f"same={same_ok} isolated_leak={leaked}"


@probe("sessions", "Follow-up Agent.run on the same session runs a new turn (no replay)", scope="local", cost_turns=3)
def followup_not_replayed(ctx: ProbeContext) -> str:
    """Deterministic version of the continuity check, on the fake provider.

    The fake echoes the user's message, so a genuinely new turn must contain
    the new message and must reach the provider.
    """
    fake = ctx.extras.get("fake")
    if fake is None:
        raise ProbeSkipped("needs the local fake provider")
    agent = ctx.agent(agent_id="local", tools=[])  # no default tool catalog: keeps the fake echo clean
    session = ctx.session("followup")
    ctx.turn(agent, "first message ALPHA", label="first", session_id=session)
    outcomes = []
    for index, word in enumerate(("BRAVO", "CHARLIE"), start=2):
        mark = len(fake.requests)
        result, record = ctx.turn(agent, f"message number {index} {word}", label=f"followup-{index}", session_id=session)
        calls = len(fake.requests) - mark
        text = getattr(result, "text", record.error)
        outcomes.append((word, calls, text))
        ctx.check(f"follow-up {index} reached the provider", calls > 0, f"provider requests={calls}")
        ctx.check(f"follow-up {index} answers the new message", has(text, word), f"text={text[:160]!r}")
    return "; ".join(f"{w}: calls={c} text={t[:60]!r}" for w, c, t in outcomes)
