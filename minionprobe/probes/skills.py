"""Skills: a SKILL.md ingested with the CLI is stored, admitted and applied.

The runner prepares each home in two ways (see runner._prepare_model_home):
1. the documented default, ``openminion skill ingest --file SKILL.md``;
2. the path that works on 0.1.8: ``--name`` plus ``skill admit`` with
   ``--expected-active-version-hash none`` and verification evidence.
"""

from __future__ import annotations

from ..core import ProbeContext, ProbeSkipped, probe


@probe("skills", "Ingested SKILL.md keeps its skill_id, is admitted and applied")
def ingested_skill_applied(ctx: ProbeContext) -> str:
    info = ctx.extras.get("skill")
    if not info:
        raise ProbeSkipped("probe skill was not ingested for this home")
    ctx.check(
        "default ingest keeps the skill_id declared in the file",
        info["default_ok"] and str(info["default_skill_id"]).replace("_", ".") == "minionprobe.release.codename",
        f"skill_id={info['default_skill_id']!r} warnings={info['default_warnings']}",
    )
    ctx.check("ingest --name + admit (verified) succeeds", info["admit_ok"], info["admit_error"] or f"skill_id={info['named_skill_id']}")
    ctx.check("admitted skill appears in `openminion skill list`", info["listed"], info["list_output"])
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
    ctx.observe("skill_turn", purposes=record.purposes, http_requests=record.http.get("requests"))
    return f"default_skill_id={info['default_skill_id']} admitted={info['admit_ok']} codename_in_reply={applied}"
