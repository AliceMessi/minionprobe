"""Triage: classify failures with an OpenMinion agent and draft issue reports.

The triage step is itself built on the SDK:

* an ``Agent`` with three read-only ``@tool`` functions over the probe
  evidence (``list_failures``, ``get_failure``, ``search_known_issues``);
* a ``SKILL.md`` rubric, ingested with ``openminion skill ingest`` and also
  given as instructions;
* ``output_type=TriageReport`` for a typed result;
* a bounded ``subagent`` ("issue-writer") that drafts a GitHub issue body for
  each new OpenMinion bug.

Drafts are written to ``results/<date>/triage/drafts/`` with a
"not filed" banner. Nothing is posted anywhere: a human reviews each draft
and files it by hand.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .env import PACKAGE_DIR, PROVIDERS, REPO_ROOT, ingest_skill, isolation_env, new_home, setup_agent
from .redact import redact

RUBRIC = PACKAGE_DIR / "skills" / "triage-rubric" / "SKILL.md"
KNOWN = json.loads((PACKAGE_DIR / "data" / "known_issues.json").read_text(encoding="utf-8"))

# Probe -> verified, human-reviewed draft in issue-drafts/ (see README there).
VERIFIED_DRAFTS = {
    "diagnostics.home_isolation": "issue-drafts/01-identity-db-written-to-user-home.md",
    "sessions.followup_not_replayed": "issue-drafts/02-followup-run-replays-previous-reply.md",
    "sessions.continuity_isolation": "issue-drafts/02-followup-run-replays-previous-reply.md",
    "models.session_agent_binding": "issue-drafts/03-agent-id-ignored-for-existing-session.md",
    "diagnostics.rate_limit_after_tool": "issue-drafts/04-429-after-tool-returned-as-completed-text.md",
    "tasks.cron_schedules": "issue-drafts/05-cron-unknown-timezone-on-windows-tzdata.md",
    "tools.decorated_tool": "issue-drafts/06-closure-judge-overwrites-grounded-tool-answer.md",
}

Category = Literal["known_issue", "openminion_bug", "model_behavior", "provider_limitation", "probe_bug"]


class TriageItem(BaseModel):
    key: str
    category: Category
    known_ref: str | None = None
    confidence: float = Field(ge=0, le=1)
    rationale: str


class TriageReport(BaseModel):
    items: list[TriageItem]


def _failures(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for result in payload.get("results", []):
        if result["status"] in {"FAIL", "ERROR", "BLOCKED"}:
            out.append(result)
    return out


def _key(result: dict[str, Any]) -> str:
    return f"{result['provider']}/{result['probe']}"


def _compact(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": _key(result),
        "status": result["status"],
        "title": result["title"],
        "failed_checks": [f"{c['name']} | {c['detail'][:160]}" for c in result.get("checks", []) if not c["ok"]],
        "known_issues_seen": result.get("known_issues", []),
        "error": (result.get("error") or "").splitlines()[0][:200] if result.get("error") else "",
    }


def rule_triage(result: dict[str, Any]) -> TriageItem:
    """Deterministic first pass; also the fallback when no model is used."""
    key = _key(result)
    if result["status"] == "BLOCKED":
        return TriageItem(key=key, category="provider_limitation", confidence=0.9, rationale=f"provider blocked the probe: {result.get('error', '')[:120]}")
    if result.get("known_issues"):
        ref = result["known_issues"][0]
        return TriageItem(key=key, category="known_issue", known_ref=ref, confidence=0.8, rationale=f"matches upstream {ref}")
    if result["probe"] in VERIFIED_DRAFTS:
        return TriageItem(key=key, category="openminion_bug", confidence=0.7, rationale=f"matches verified draft {VERIFIED_DRAFTS[result['probe']]}")
    return TriageItem(key=key, category="model_behavior", confidence=0.4, rationale="no runtime-level signal; needs human review")


ISSUE_WRITER = """You draft GitHub issues for the OpenMinion maintainers.
Write GitHub-flavoured Markdown with exactly these sections:
## Summary, ## Environment, ## Steps to reproduce, ## Expected, ## Actual, ## Impact, ## Evidence.
Use only facts present in the evidence. Do not invent version numbers, stack traces or file paths.
Keep it under 350 words. Start with a one-line title prefixed by "TITLE: "."""


def triage(results_path: Path, *, provider: str = "gemini", max_drafts: int = 3, offline: bool = False) -> Path:
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    failures = _failures(payload)
    by_key = {_key(r): r for r in failures}
    out_dir = results_path.parent / "triage"
    drafts_dir = out_dir / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    rule_items = {k: rule_triage(r) for k, r in by_key.items()}
    model_items: dict[str, TriageItem] = {}
    notes: list[str] = []
    drafted: list[str] = []

    if failures and not offline:
        model_items, drafted, notes = _model_triage(payload, by_key, provider, max_drafts, drafts_dir)

    report = out_dir / f"TRIAGE-{results_path.stem}.md"
    lines = [
        f"# Triage of `{results_path.name}`",
        "",
        f"- Failures triaged: {len(failures)}",
        f"- Triage agent: {'offline rules only' if offline or not failures else provider + ' (' + PROVIDERS[provider].model + ')'}",
        "- Nothing here has been filed upstream. Every draft needs human review first.",
        "",
        "| Failure | Rule triage | Agent triage | Agent rationale |",
        "|---|---|---|---|",
    ]
    for key in by_key:
        rule = rule_items[key]
        agent = model_items.get(key)
        agent_cell = f"{agent.category}{' ' + agent.known_ref if agent and agent.known_ref else ''} ({agent.confidence:.1f})" if agent else "-"
        lines.append(
            f"| `{key}` | {rule.category}{' ' + rule.known_ref if rule.known_ref else ''} | {agent_cell} | {redact(agent.rationale)[:200] if agent else ''} |"
        )
    if drafted:
        lines += ["", "## Agent-drafted issues (unreviewed)", ""] + [f"- `{p}`" for p in drafted]
    lines += ["", "## Verified drafts (human-reviewed, in `issue-drafts/`)", ""]
    for probe_id, draft in sorted(set(VERIFIED_DRAFTS.items()), key=lambda x: x[1]):
        lines.append(f"- `{probe_id}` -> `{draft}`")
    if notes:
        lines += ["", "## Notes from the triage run", ""] + [f"- {n}" for n in notes]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def _model_triage(
    payload: dict[str, Any],
    by_key: dict[str, dict[str, Any]],
    provider: str,
    max_drafts: int,
    drafts_dir: Path,
) -> tuple[dict[str, TriageItem], list[str], list[str]]:
    from openminion import Agent, AgentOutputValidationError, APIRuntime, ProviderError, subagent, tool

    for name in ("openminion", "httpx", "alembic"):
        logging.getLogger(name).setLevel(logging.ERROR)
    spec = PROVIDERS[provider]
    home = new_home(REPO_ROOT / ".probe-home", f"triage-{uuid.uuid4().hex[:6]}")
    setup_agent(home, spec.agent_id, spec)
    ingest = ingest_skill(home, RUBRIC)
    os.environ.update(isolation_env(home))
    notes = [f"rubric skill ingest exit code {ingest.returncode}"]

    @tool
    def list_failures() -> dict:
        """List every non-passing probe result with its failed checks."""
        return {"failures": [_compact(r) for r in by_key.values()]}

    @tool
    def get_failure(key: str) -> dict:
        """Full redacted evidence for one failure key such as 'gemini/tools.decorated_tool'."""
        result = by_key.get(key)
        if result is None:
            return {"error": f"unknown key {key}", "keys": list(by_key)}
        turns = [
            {k: t.get(k) for k in ("label", "run_state", "latency_s", "text", "error", "purposes")} | {"http": {k: t["http"].get(k) for k in ("requests", "by_status", "by_kind", "errors")}}
            for t in result.get("turns", [])
        ]
        return {"result": _compact(result), "observations": result.get("observations", [])[:6], "turns": turns[:4]}

    @tool
    def search_known_issues(query: str) -> dict:
        """Search upstream OpenMinion issues already filed (do not re-file these)."""
        words = [w for w in query.lower().replace("/", " ").replace(".", " ").split() if len(w) > 3]
        hits = [k for k in KNOWN if any(w in (k["title"] + " " + " ".join(k.get("keywords", []))).lower() for w in words)]
        return {"matches": hits[:5], "note": "Only these upstream issues are known; anything else may be new."}

    runtime = APIRuntime.from_config_path(str(home.config_path), home_root=home.root, data_root=home.data_root)
    items: dict[str, TriageItem] = {}
    drafted: list[str] = []
    try:
        triager = Agent(
            runtime=runtime,
            agent_id=spec.agent_id,
            name="triager",
            instructions="You triage MinionProbe failures.\n\n" + RUBRIC.read_text(encoding="utf-8"),
            tools=[list_failures, get_failure, search_known_issues],
            forced_tools=["list_failures"],
            output_type=TriageReport,
        )
        try:
            # one fresh session per run: follow-up runs on a session replay (issue-drafts/02)
            result = triager.run(
                "Triage every MinionProbe failure. Return the TriageReport JSON only.",
                session_id=f"triage-{uuid.uuid4().hex[:8]}",
                timeout_seconds=240,
            )
            items = {i.key: i for i in result.output.items if i.key in by_key}
            notes.append(f"triage agent classified {len(items)}/{len(by_key)} failures; stats={result.stats}")
        except AgentOutputValidationError as exc:
            notes.append(f"triage output did not validate (AgentOutputValidationError): {redact(exc.raw_text[:200])!r}")
        except ProviderError as exc:
            notes.append(f"triage agent provider error {exc.code}: {redact(exc.message[:200])}")

        candidates = [k for k, i in items.items() if i.category == "openminion_bug" and not i.known_ref][:max_drafts]
        writer = subagent(triager, name="issue-writer", instructions=ISSUE_WRITER, timeout_seconds=180)
        for key in candidates:
            evidence = json.dumps(get_failure(key), default=str)[:6000]
            env_line = f"openminion {payload.get('openminion_version')}, Python {payload.get('python')}, {payload.get('platform')}"
            try:
                draft = writer.run(
                    f"Environment: {env_line}\nProbe: {key}\nEvidence (redacted JSON):\n{evidence}",
                    session_id=f"draft-{uuid.uuid4().hex[:8]}",
                    timeout_seconds=180,
                )
            except ProviderError as exc:
                notes.append(f"issue-writer failed for {key}: {exc.code}")
                continue
            path = drafts_dir / (key.replace("/", "__") + ".md")
            path.write_text(
                "<!-- DRAFT generated by MinionProbe's issue-writer subagent. NOT FILED. "
                "A human must verify every claim before filing. -->\n\n" + redact(draft.text) + "\n",
                encoding="utf-8",
            )
            drafted.append(str(path.relative_to(REPO_ROOT)))
    finally:
        runtime.close()
    return items, drafted, notes
