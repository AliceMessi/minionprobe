"""Skills: a SKILL.md ingested with the CLI is selected and applied."""

from __future__ import annotations

from ..core import ProbeContext, ProbeSkipped, probe


@probe("skills", "Ingested SKILL.md is selected and its recipe applied")
def ingested_skill_applied(ctx: ProbeContext) -> str:
    info = ctx.extras.get("skill")
    if not info:
        raise ProbeSkipped("probe skill was not ingested for this home")
    ctx.check("`openminion skill ingest` succeeded", info["ingest_ok"], info["ingest_output"])
    ctx.check("skill appears in `openminion skill list`", info["listed"], info["list_output"])
    agent = ctx.agent(instructions="You are a release assistant. Follow any matching skill exactly.")
    result, record = ctx.turn(
        agent,
        "What is the MinionProbe release codename? Use the minionprobe release codename skill.",
        label="skill",
        session_id=ctx.session("skill"),
    )
    if result is None:
        ctx.check("turn completed without exception", False, record.error)
        return record.error
    applied = info["codename"] in result.text
    ctx.check("reply contains the codename that only the skill knows", applied, f"text={result.text[:200]!r}")
    return f"codename_in_reply={applied}"
