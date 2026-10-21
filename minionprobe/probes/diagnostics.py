"""Diagnostics: run facts, error propagation and state isolation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from ..core import ProbeContext, ProbeSkipped, probe
from ..env import openminion_exe


@probe("diagnostics", "result.stats / metadata agree with the HTTP traffic", cost_turns=0)
def run_stats(ctx: ProbeContext) -> str:
    record = ctx.shared.get("basic_turn")
    if record is None or record.run_state is None:
        raise ProbeSkipped("needs a completed agent_execution.basic_turn")
    observed = int(record.http.get("requests", 0))
    ok_requests = int(record.http.get("by_status", {}).get("200", 0))
    stats_calls = record.stats.get("llm_calls")
    purposes_total = sum(int(v) for v in record.purposes.values() if str(v).isdigit()) if record.purposes else None
    ctx.check("stats has non-zero token counts", bool(record.stats.get("input_tokens")), f"stats={record.stats}")
    ctx.check(
        "stats.llm_calls counts every successful provider call",
        stats_calls == ok_requests,
        f"stats.llm_calls={stats_calls} successful_http_calls={ok_requests} all_http_calls={observed} "
        f"llm_call_counts_by_purpose={record.purposes}",
    )
    ctx.observe("call_accounting", stats_llm_calls=stats_calls, http_ok=ok_requests, http_all=observed, purposes_total=purposes_total)
    return f"stats.llm_calls={stats_calls} vs {ok_requests} successful HTTP calls ({observed} total)"


@probe("diagnostics", "Unreachable provider raises ProviderError (no fake success)", scope="local", cost_turns=1)
def provider_unreachable(ctx: ProbeContext) -> str:
    agent = ctx.agent(agent_id="broken")
    result, record = ctx.turn(agent, "hello", label="closed-port", session_id=ctx.session("broken"), timeout_seconds=60)
    raised = result is None and record.error_code not in {"", "TurnTimeoutError"}
    ctx.check(
        "ProviderError raised for a closed port",
        raised and record.error.startswith("ProviderError"),
        record.error or f"run_state={getattr(result, 'run_state', None)} text={getattr(result, 'text', '')[:160]!r}",
    )
    return record.error[:200] if record.error else f"returned text: {getattr(result, 'text', '')[:120]!r}"


@probe("diagnostics", "429 on the call after a tool runs raises ProviderError", scope="local", cost_turns=1)
def rate_limit_after_tool(ctx: ProbeContext) -> str:
    from openminion import tool

    from ..fakeprovider import Fault

    fake = ctx.extras.get("fake")
    if fake is None:
        raise ProbeSkipped("needs the local fake provider")
    calls: list[str] = []

    @tool
    def lookup_order(order_id: str) -> str:
        """Return the status of one order."""
        calls.append(order_id)
        return f"order {order_id}: ready"

    fault = Fault(status=429, when="after_tool")
    fake.script.faults = [fault]
    try:
        agent = ctx.agent(agent_id="local", tools=[lookup_order], forced_tools=["lookup_order"])
        result, record = ctx.turn(
            agent,
            "Check order A-104",
            label="429-after-tool",
            session_id=ctx.session("rl"),
            max_rate_retries=0,
            raw_rate_limit=True,
        )
    finally:
        fake.script.faults = []
    ctx.check("tool executed before the fault", bool(calls), f"calls={calls}")
    ctx.check("fault was injected", fault.hits > 0, f"429 responses served={fault.hits}")
    ctx.check(
        "ProviderError(RATE_LIMITED) raised",
        result is None and "RATE_LIMITED" in record.error,
        record.error
        or f"run_state={getattr(result, 'run_state', None)} text={getattr(result, 'text', '')[:200]!r}",
    )
    return f"run_state={getattr(result, 'run_state', None)} text={getattr(result, 'text', record.error)[:160]!r}"


_LEAK_SCRIPT = textwrap.dedent(
    """
    import logging, sys, uuid
    from pathlib import Path
    logging.disable(logging.CRITICAL)
    from openminion import Agent, APIRuntime
    proj = Path(sys.argv[1])
    rt = APIRuntime.from_config_path(str(proj / ".openminion" / "agents.json"),
                                     home_root=proj, data_root=proj / ".openminion")
    with Agent(runtime=rt, session_id="leak-" + uuid.uuid4().hex[:8]) as agent:
        result = agent.run("hello")
    rt.close()
    print("RUN_STATE=" + str(result.run_state))
    """
)


@probe("diagnostics", "Explicit home_root/data_root keep all state out of ~/.openminion", scope="local", cost_turns=0)
def home_isolation(ctx: ProbeContext) -> str:
    """Runs a credential-free echo turn in a subprocess whose user profile is
    a temporary directory, then looks for anything written to that fake home."""
    with tempfile.TemporaryDirectory(prefix="mp-iso-") as tmp:
        tmp_path = Path(tmp)
        fake_home, project = tmp_path / "fakehome", tmp_path / "project"
        fake_home.mkdir()
        project.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENMINION_")}
        env.update({"USERPROFILE": str(fake_home), "HOME": str(fake_home), "PYTHONIOENCODING": "utf-8"})
        init = subprocess.run(  # noqa: S603
            [*openminion_exe(), "--home-root", str(project), "--data-root", str(project / ".openminion"),
             "--no-update-check", "config", "init", "--provider", "echo"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        ctx.check("echo config initialised in the project root", init.returncode == 0, init.stdout[-200:])
        script = tmp_path / "leak_check.py"
        script.write_text(_LEAK_SCRIPT, encoding="utf-8")
        run = subprocess.run(  # noqa: S603
            [sys.executable, "-I", str(script), str(project)],
            capture_output=True, text=True, env=env, timeout=180,
        )
        ctx.check("echo turn completed", "RUN_STATE=completed" in run.stdout, (run.stdout + run.stderr)[-300:])
        leaked = sorted(str(p.relative_to(fake_home)) for p in fake_home.rglob("*") if p.is_file())
        ctx.check("nothing written under the user's home", not leaked, json.dumps(leaked))
        ctx.observe("files_written_to_home", files=leaked)
    return f"files written under fake home: {leaked}"
