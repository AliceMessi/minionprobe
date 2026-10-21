"""Tasks / scheduling: CLI cron jobs and the model-facing task.schedule tool."""

from __future__ import annotations

import json
from typing import Any

from ..core import ProbeContext, probe


def _cron_create(ctx: ProbeContext, *schedule: str, name: str) -> dict[str, Any]:
    proc = ctx.home.cli(
        "cron", "create", "--instruction", "minionprobe scheduled check", *schedule, "--name", name, "--json"
    )
    text = proc.stdout.strip()
    # the JSON document is printed last; earlier lines may be log output
    start = 0 if text.startswith("{") else text.find("\n{") + 1
    try:
        payload = json.loads(text[start:]) if text else {}
    except json.JSONDecodeError:
        payload = {}
    error = str(payload.get("error", "")) if payload else f"rc={proc.returncode} {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ''}"
    return {"rc": proc.returncode, "ok": bool(payload.get("ok")), "error": error[:240]}


@probe("tasks", "`openminion cron create` accepts cron/at schedules and timezones", scope="local", cost_turns=0)
def cron_schedules(ctx: ProbeContext) -> str:
    try:
        import tzdata  # noqa: F401

        has_tzdata = True
    except ImportError:
        has_tzdata = False
    cases = {
        "one-shot --at": ("--at", "2026-12-31T09:00:00+00:00"),
        "--cron without --tz (default UTC)": ("--cron", "0 9 * * 1-5"),
        "--cron --tz UTC": ("--cron", "0 9 * * 1-5", "--tz", "UTC"),
        "--cron --tz Europe/Rome": ("--cron", "0 9 * * 1-5", "--tz", "Europe/Rome"),
    }
    outcomes = {}
    for index, (label, schedule) in enumerate(cases.items()):
        outcome = _cron_create(ctx, *schedule, name=f"mp-{ctx.run_id}-{index}")
        outcomes[label] = outcome
        ctx.check(f"cron create {label}", outcome["ok"], outcome["error"] or "ok")
    ctx.observe("tzdata_installed", value=has_tzdata)
    status = ctx.home.cli("cron", "status")
    ctx.observe("cron_status", output=status.stdout.strip()[-300:])
    return "; ".join(f"{k}: {'ok' if v['ok'] else v['error']}" for k, v in outcomes.items())


@probe("tasks", "Model schedules a one-shot job through the task.schedule tool")
def task_schedule_tool(ctx: ProbeContext) -> str:
    name = ctx.nonce("mp-reminder").lower()
    approvals: list[str] = []

    def approve(tool_name: str, args: dict[str, Any], approval_id: str) -> bool:
        approvals.append(tool_name)
        return True

    agent = ctx.agent(
        instructions="You schedule jobs. Always use the task.schedule tool; never just describe it.",
        tools=["task.schedule"],
        forced_tools=["task.schedule"],
    )
    result, record = ctx.turn(
        agent,
        (
            f"Schedule a one-time job named {name} at 2026-12-31T09:00:00Z with the instruction "
            "'Post the MinionProbe nightly matrix'. Use schedule kind 'at'."
        ),
        label="task.schedule",
        session_id=ctx.session("task"),
        approval_callback=approve,
    )
    tool_results = [
        str(m.get("content", ""))
        for e in ctx.last_events
        for m in (e._messages or [])
        if m.get("role") == "tool"
    ]
    scheduled = any('"ok": true' in t.replace("'", '"').lower() or "task_id" in t for t in tool_results)
    ctx.check("task.schedule tool ran and reported a task id", scheduled, (tool_results[-1] if tool_results else "no tool result")[:300])
    listing = ctx.home.cli("tasks", "list")
    ctx.check("job is visible to `openminion tasks list`", name in listing.stdout, listing.stdout.strip()[-300:])
    if approvals:
        ctx.observe("approval_requested", tools=approvals)
    return f"scheduled={scheduled} approvals={approvals} reply={getattr(result, 'text', record.error)[:120]!r}"
