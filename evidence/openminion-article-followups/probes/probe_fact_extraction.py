"""Auto fact extraction: an extra provider request before the agent's own request, on by default.

Installed package only (no repo test fixtures); run with `python -I -X utf8`
from an empty directory outside any checkout. It reuses the #448 probe's
helpers (`probe_420_lib.py`, `_common.py`, unchanged): a real `APIRuntime` for
one agent, `openminion`, whose `openai` provider is a scripted
OpenAI-compatible server on 127.0.0.1. This probe subclasses that server to
record every request in full (order, purpose, tools offered, tool_choice,
model, size, whether the agent's instructions are in it) and to answer the
agent's own (non-forced) requests with a fixed reply.

One `Agent(instructions=..., tools=[probe.lookup_status]).run(MESSAGE)` turn per
case, each in a fresh runtime home. MESSAGE is a plain task: nothing in it is
worth remembering, and it is longer than the 8-character extraction minimum.
  D  default agents.json (no `brain` section)
  B  top-level `"brain": {"auto_fact_extraction": {"enabled": false}}`
  A  the same flag under `agents.openminion` (where the brain's AgentProfile
     schema also has a field of that name); recorded, not checked

The checks state the expected behaviour; a FAIL marks the defect.

Usage: python -I -X utf8 probe.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import traceback
from importlib import metadata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()


def _dist(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _direct_url() -> Any:
    try:
        text = metadata.distribution("openminion").read_text("direct_url.json")
        return json.loads(text) if text else None
    except Exception:  # noqa: BLE001
        return None


info = banner("probe_auto_fact_extraction",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic")},
              openminion_direct_url=_direct_url())

from openminion import Agent, tool  # noqa: E402

import probe_420_lib as lib  # noqa: E402
from probe_420_lib import AGENT_ID, ScriptedProvider, chdir_env, restore_env  # noqa: E402

INSTRUCTIONS_MARKER = "PROBE-AGENT-INSTRUCTIONS-7f3c"
INSTRUCTIONS = (f"{INSTRUCTIONS_MARKER}. You are the release assistant. Call probe.lookup_status for the "
                "project you are given, then reply with one short line. Never invent a status.")
MESSAGE = "Look up the release status of project Falcon and summarise it in one line."
REPLY = "PROBE-REPLY: Falcon is on track for release."
EXTRACTION_DELAY_S = 0.5  # makes the wait for the extraction visible in the timings
AFE_CONTRACT_TEXT = "candidate facts, user preferences, or tasks"
c = Checks()


@tool(name="probe.lookup_status", min_scope="READ_ONLY", idempotent=True)
def lookup_status(project: str) -> str:
    """Return the release status of a project."""
    return json.dumps({"project": project, "status": "on track"})


def _messages_text(body: dict[str, Any], role: str | None = None) -> str:
    parts = []
    for message in body.get("messages") or []:
        if role is not None and message.get("role") != role:
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        parts.append(str(content or ""))
    return "\n".join(parts)


class RecordingProvider(ScriptedProvider):
    """The #448 probe's scripted server; records each request in full and answers the agent's own
    (non-forced) requests with REPLY. Forced `submit_output` requests keep the base answers
    (the extraction gets an empty `items` list, since MESSAGE is not the base capture message)."""

    def __init__(self) -> None:
        self.detail: list[dict[str, Any]] = []
        self.detail_lock = threading.Lock()
        super().__init__()

    def respond(self, body: dict[str, Any], path: str) -> dict[str, Any]:
        t_recv = time.monotonic()
        txt = lib._all_text(body)
        choice = body.get("tool_choice")
        forced = str((choice.get("function") or {}).get("name", "")) if isinstance(choice, dict) else ""
        tools = [str((t.get("function") or {}).get("name", "")) for t in body.get("tools") or []
                 if isinstance(t, dict)]
        purposes = sorted(set(re.findall(r"purpose:\s*([a-z_]+)", txt)))
        is_extraction = forced == "submit_output" and (AFE_CONTRACT_TEXT in txt or "purpose: reflect" in txt)
        row: dict[str, Any] = {
            "kind": "extraction" if is_extraction else ("agent_text" if not forced else f"forced:{forced}"),
            "purposes_in_text": purposes,
            "afe_contract_in_text": AFE_CONTRACT_TEXT in txt,
            "model": body.get("model"),
            "tools": tools,
            "tool_choice": choice if choice is not None else "<absent>",
            "messages": len(body.get("messages") or []),
            "request_chars": len(json.dumps(body)),
            "system_chars": len(_messages_text(body, "system")),
            "has_agent_instructions": INSTRUCTIONS_MARKER in txt,
            "has_user_message": MESSAGE in txt,
            "t_recv": t_recv,
        }
        if is_extraction:
            time.sleep(EXTRACTION_DELAY_S)
        if is_extraction:  # nothing in MESSAGE is worth remembering
            answer = lib._submit({"items": []}, f"call-extract-{len(self.detail)}")
        elif forced:
            answer = super().respond(body, path)
        else:
            answer = lib._text(REPLY)
        row["t_sent"] = time.monotonic()
        row["answer"] = (answer["choices"][0]["message"].get("content")
                         or [tc["function"]["arguments"] for tc in answer["choices"][0]["message"].get("tool_calls") or []])
        with self.detail_lock:
            row["index"] = len(self.detail)
            self.detail.append(row)
        return answer


def build_runtime(root: Path, provider: ScriptedProvider, *, patch: dict[str, Any] | None) -> Any:
    """`probe_420_lib.build_runtime` without the identity/memory options, plus a JSON patch of agents.json
    applied after `save_config`, exactly as a user edits the file."""
    from openminion import APIRuntime
    from openminion.base.config import AgentProfileConfig, ConfigManager, OpenMinionConfig, save_config

    root.mkdir(parents=True, exist_ok=True)
    config = OpenMinionConfig()
    config.agents = {AGENT_ID: AgentProfileConfig(name=AGENT_ID, provider="openai", default_channel="console")}
    config.default_agent = AGENT_ID
    config.providers.openai.api_key_env = "PROBE_420_PROVIDER_KEY"
    config.providers.openai.base_url = provider.base_url
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / ".openminion" / "state.db")
    path = root / "agents.json"
    save_config(config, str(path))
    if patch:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "brain" in patch:
            raw["brain"] = patch["brain"]
        for agent_id, extra in (patch.get("agents") or {}).items():
            raw.setdefault("agents", {}).setdefault(agent_id, {}).update(extra)
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    manager = ConfigManager.load(str(path), home_root=root, data_root=root / ".openminion")
    return APIRuntime.from_manager(manager)


def _plain(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v, depth + 1) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _plain(dump(), depth + 1)
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "__dict__"):
        return {k: _plain(v, depth + 1) for k, v in vars(value).items() if not k.startswith("_")}
    return repr(value)


def afe_config_seen_by_runner(runtime: Any) -> Any:
    try:
        runner = runtime.resolve_agent_service(AGENT_ID)._get_runner()
        cfg = getattr(getattr(runner, "profile", None), "auto_fact_extraction", None)
        return _plain(cfg)
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def run_case(label: str, patch: dict[str, Any] | None) -> dict[str, Any]:
    root = WORK / label
    provider = RecordingProvider()
    old_env = chdir_env(root)
    row: dict[str, Any] = {"case": label, "agents_json_patch": patch}
    runtime = None
    approvals: list[str] = []

    def approve(*a: Any) -> bool:
        approvals.append(str(a[0]) if a else "")
        return True

    try:
        runtime = build_runtime(root, provider, patch=patch)
        row["runner_profile_auto_fact_extraction"] = afe_config_seen_by_runner(runtime)
        t0 = time.monotonic()
        with Agent(runtime=runtime, instructions=INSTRUCTIONS, tools=[lookup_status]) as agent:
            res = agent.run(MESSAGE, session_id=f"afe-{label}", timeout_seconds=120, approval_callback=approve)
        row["wall_s"] = round(time.monotonic() - t0, 3)
        row["run_state"] = getattr(res, "run_state", None)
        row["text"] = getattr(res, "text", None)
        row["stats"] = _plain(getattr(res, "stats", None))
        row["approval_callback_calls"] = approvals
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        row["case_error"] = f"{type(exc).__name__}: {exc}"
        row["case_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                    for f in traceback.extract_tb(exc.__traceback__)][-8:]
    finally:
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:  # noqa: BLE001
                row["runtime_close"] = f"{type(exc).__name__}: {exc}"
        restore_env(old_env)
        provider.close()
    with provider.detail_lock:
        reqs = sorted(provider.detail, key=lambda r: r["t_recv"])
    base = reqs[0]["t_recv"] if reqs else 0.0
    for r in reqs:
        r["t_recv"] = round(r["t_recv"] - base, 3)
        r["t_sent"] = round(r["t_sent"] - base, 3)
    row["requests"] = reqs
    return row


cases = {
    "D": run_case("D", None),
    "B": run_case("B", {"brain": {"auto_fact_extraction": {"enabled": False}}}),
    "A": run_case("A", {"agents": {AGENT_ID: {"auto_fact_extraction": {"enabled": False}}}}),
}


def extraction(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in case.get("requests", []) if r["kind"] == "extraction"]


def order(case: dict[str, Any]) -> list[str]:
    return [f"{r['kind']} purposes={r['purposes_in_text']} tools={len(r['tools'])}" for r in case.get("requests", [])]


d, b, a = cases["D"], cases["B"], cases["A"]
d_ex, b_ex, a_ex = extraction(d), extraction(b), extraction(a)
d_reqs, b_reqs = d.get("requests", []), b.get("requests", [])
first_agent_d = next((r for r in d_reqs if r["kind"] != "extraction"), None)

hosts = sorted({h for case in cases.values() for h in case.get("provider_hosts", [])})
errors = {k: case.get("case_error") for k, case in cases.items() if case.get("case_error")}
c.check("Offline provider and no case error: every case's openai base URL is the scripted server on 127.0.0.1",
        hosts == ["127.0.0.1"] and not errors, {"hosts": hosts, "errors": errors})

c.check("B (control, top-level brain.auto_fact_extraction.enabled=false): the turn makes no extraction request "
        "and returns the agent's reply",
        not b_ex and REPLY in str(b.get("text") or ""),
        {"order": order(b), "text": b.get("text"), "run_state": b.get("run_state"),
         "runner_profile_auto_fact_extraction": b.get("runner_profile_auto_fact_extraction")})

c.check("D vs B: the extraction does not change the visible reply (same text with and without it)",
        bool(d.get("text")) and d.get("text") == b.get("text"),
        {"D": d.get("text"), "B": b.get("text")})

c.check("D: if an extraction request is sent, it offers only submit_output and forces it "
        "(tool_choice = function submit_output), so a model cannot answer it in prose",
        all(r["tools"] == ["submit_output"] and isinstance(r["tool_choice"], dict)
            and (r["tool_choice"].get("function") or {}).get("name") == "submit_output" for r in d_ex),
        [{k: r[k] for k in ("index", "tools", "tool_choice", "model", "answer")} for r in d_ex])

c.check("D (default config): the turn's first provider request is the agent's own request, "
        "not an extraction request offering only submit_output",
        bool(d_reqs) and d_reqs[0]["kind"] != "extraction",
        {"order": order(d),
         "first_request": {k: d_reqs[0][k] for k in ("kind", "tools", "tool_choice", "model", "request_chars",
                                                     "has_agent_instructions", "t_recv", "t_sent")} if d_reqs else None,
         "first_agent_request_t_recv": first_agent_d["t_recv"] if first_agent_d else None})

c.check("D (default config): a turn whose message holds nothing to remember makes no more provider requests "
        "than the same turn with extraction disabled (B)",
        bool(d_reqs) and bool(b_reqs) and len(d_reqs) <= len(b_reqs),
        {"D_requests": len(d_reqs), "B_requests": len(b_reqs), "D_extraction_requests": len(d_ex),
         "D_extraction_answers": [r["answer"] for r in d_ex],
         "D_request_chars_total": sum(r["request_chars"] for r in d_reqs),
         "B_request_chars_total": sum(r["request_chars"] for r in b_reqs),
         "D_wall_s": d.get("wall_s"), "B_wall_s": b.get("wall_s")})

c.check("D: the extraction request does not carry the agent's instructions (they ask for tools it does not offer)",
        not any(r["has_agent_instructions"] for r in d_ex),
        [{k: r[k] for k in ("index", "has_agent_instructions", "has_user_message", "request_chars", "system_chars",
                            "messages", "model")} for r in d_ex]
        + [{"first_agent_request": {k: first_agent_d[k] for k in ("request_chars", "system_chars", "model", "tools")}
            if first_agent_d else None}])

observed = {
    "cases": cases,
    "summary": {
        label: {"order": order(case), "extraction_requests": len(extraction(case)),
                "requests": len(case.get("requests", [])), "text": case.get("text"),
                "run_state": case.get("run_state"), "wall_s": case.get("wall_s"),
                "runner_profile_auto_fact_extraction": case.get("runner_profile_auto_fact_extraction"),
                "stats": case.get("stats")}
        for label, case in cases.items()
    },
    "info_A_agent_level_flag": {
        "note": "agents.openminion.auto_fact_extraction is not a parsed agent field; recorded only",
        "extraction_requests": len(a_ex), "order": order(a),
        "runner_profile_auto_fact_extraction": a.get("runner_profile_auto_fact_extraction"),
        "case_error": a.get("case_error")},
}
write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": observed})
summary = c.summary()
for label in ("D", "B", "A"):
    print(f"{label}:", json.dumps(observed["summary"][label], default=str)[:1500])
print("A (agent-level flag, info):", json.dumps(observed["info_A_agent_level_flag"], default=str))
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
