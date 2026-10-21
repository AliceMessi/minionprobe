"""Markdown rendering for the provider x building-block matrix."""

from __future__ import annotations

from typing import Any

from .core import BLOCK_TITLES, BLOCKS

STATUS_ORDER = ["PASS", "FAIL", "ERROR", "BLOCKED", "SKIP"]


def _cell(result: dict[str, Any] | None) -> str:
    if result is None:
        return "-"
    status = result["status"]
    cell = f"**{status}** {result['latency_s']:.0f}s"
    if result.get("known_issues"):
        cell += " (" + ", ".join(result["known_issues"]) + ")"
    return cell


def _failed_checks(result: dict[str, Any]) -> list[str]:
    return [f"{c['name']}: {c['detail']}" if c.get("detail") else c["name"] for c in result.get("checks", []) if not c["ok"]]


def render_matrix(payload: dict[str, Any]) -> str:
    results = payload.get("results", [])
    providers = [p for p in payload.get("providers", {}) if any(r["provider"] == p for r in results)]
    by_key = {(r["provider"], r["probe"]): r for r in results}
    probe_ids: list[tuple[str, str, str]] = []
    for result in results:
        key = (result["block"], result["probe"], result["title"])
        if key not in probe_ids:
            probe_ids.append(key)
    block_rank = {b: i for i, (b, _) in enumerate(BLOCKS)}
    probe_ids.sort(key=lambda k: (block_rank.get(k[0], 99), k[1]))

    lines = [
        "# MinionProbe matrix",
        "",
        f"- OpenMinion: `{payload.get('openminion_version')}` | Python {payload.get('python')} | {payload.get('platform')}",
        f"- Started: {payload.get('started')} | Finished: {payload.get('finished')}",
        "",
        "| Provider | Model | Notes |",
        "|---|---|---|",
    ]
    for name in providers:
        meta = payload["providers"][name]
        lines.append(f"| {name} | `{meta.get('model')}` | {meta.get('note', '')} |")
    lines += ["", "PASS = all checks held; FAIL = a check failed; ERROR = probe crashed; BLOCKED = the provider (rate limit/quota) prevented a verdict; SKIP = not applicable. Times are wall-clock seconds, including rate-limit pacing.", ""]
    header = "| Block | Probe | " + " | ".join(providers) + " |"
    lines += [header, "|" + "---|" * (len(providers) + 2)]
    for block, probe_id, title in probe_ids:
        cells = " | ".join(_cell(by_key.get((p, probe_id))) for p in providers)
        lines.append(f"| {BLOCK_TITLES.get(block, block)} | `{probe_id.split('.', 1)[1]}`: {title} | {cells} |")

    totals = {p: {s: 0 for s in STATUS_ORDER} for p in providers}
    for result in results:
        if result["provider"] in totals:
            totals[result["provider"]][result["status"]] += 1
    lines += ["", "## Totals", "", "| Provider | " + " | ".join(STATUS_ORDER) + " |", "|---|" + "---|" * len(STATUS_ORDER)]
    for name in providers:
        lines.append(f"| {name} | " + " | ".join(str(totals[name][s]) for s in STATUS_ORDER) + " |")

    lines += ["", "## Details (non-PASS results)", ""]
    for result in sorted(results, key=lambda r: (r["provider"], r["probe"])):
        if result["status"] == "PASS":
            continue
        lines.append(f"### {result['provider']} / {result['probe']}: {result['status']}")
        for item in _failed_checks(result):
            lines.append(f"- failed check: {item}")
        if result.get("error"):
            lines.append(f"- error: `{result['error'].splitlines()[0][:300]}`")
        for obs in result.get("observations", [])[:4]:
            lines.append(f"- observation `{obs.get('kind')}`: {str({k: v for k, v in obs.items() if k != 'kind'})[:300]}")
        lines.append("")

    observations = [(r["provider"], r["probe"], o) for r in results for o in r.get("observations", [])]
    notable = [o for o in observations if o[2].get("kind") in {"provider_error_returned_as_text", "grounded_answer_replaced", "rate_limited_retry", "hidden_llm_calls"}]
    if notable:
        lines += ["## Notable observations", ""]
        for provider, probe_id, obs in notable:
            body = {k: v for k, v in obs.items() if k != "kind"}
            lines.append(f"- {provider} / `{probe_id}` `{obs['kind']}`: {str(body)[:260]}")
    return "\n".join(lines) + "\n"
