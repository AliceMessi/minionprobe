"""#347: Gemini's OpenAI-compatible endpoint gets one ordered system message;
other endpoints keep their established request shapes.

Offline. The transport is mocked below the provider adapter: `urllib.request.urlopen`
and `httpx.Client.send` are replaced, so the captured bodies are the serialized
bytes the installed adapter would put on the wire. Sockets are blocked. Credentials
are fixture values set in this process; request headers are never recorded.

  A. LLM-handle level (configs built with the installed provider-setup path, as
     `openminion setup` does): a schema-only request (system context + forced
     submit_output, the judge/repair/reflect shape) and an ordinary tool-call
     transcript (complete and stream), on the Gemini endpoint (preset, custom-named
     connection, identity derived from base URL) vs a Gemini model behind a
     non-Google gateway; plus OpenAI, MiniMax, Cortensor Portal.
  B. SDK level: the #328 scenario (decorated lookup_order, forced_tools,
     "Check order A-104") through APIRuntime/Agent on the gemini preset, the
     gateway, OpenAI and MiniMax, answered by MinionProbe's scripted fake logic.

Usage: python -I probe_347.py <work_dir> <out.json>
"""

from __future__ import annotations

import copy
import io
import json
import logging
import os
import socket
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
PROBE_REPO = Path(__file__).resolve().parents[3]  # MinionProbe repo root
sys.path.insert(0, str(PROBE_REPO))
from _common import Checks, banner, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
for name in ("GEMINI_API_KEY", "OPENAI_API_KEY", "MINIMAX_API_KEY", "CORTENSOR_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
    os.environ[name] = "fixture-key"  # never a real credential in this probe
for key in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT"):
    os.environ.pop(key, None)
quiet()
info = banner("probe_347")

import httpx  # noqa: E402

from minionprobe.fakeprovider import FakeProvider, FakeScript  # noqa: E402

MODEL = "gemini-3.1-flash-lite"
GEMINI_HOST = "generativelanguage.googleapis.com"
GATEWAY_BASE_URL = "https://llm-gateway.example.test/v1"
TOOL_OUTPUT = "order A-104: ready for pickup, pickup code ZEBRA-42"
CANDIDATE = "Order A-104 is ready for pickup. Your pickup code is ZEBRA-42."
CONTRACT = "Tool-calling contract:"


# ---------------------------------------------------------------- transport mock
_real_connect = socket.socket.connect
BLOCKED_CONNECTS: list[str] = []


def _blocked_connect(self, address, *args, **kwargs):
    host = address[0] if isinstance(address, tuple) else str(address)
    if host in ("127.0.0.1", "::1", "localhost"):  # asyncio's socketpair on Windows
        return _real_connect(self, address, *args, **kwargs)
    BLOCKED_CONNECTS.append(str(host))
    raise OSError("network blocked by probe_347")


socket.socket.connect = _blocked_connect  # type: ignore[method-assign]


class Wire:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responder = None  # callable(body) -> (status, payload)
        self._lock = threading.Lock()

    def reply(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.responder is not None:
            return self.responder(body)
        tools = body.get("tools") or []
        title = str(tools[0]["function"]["parameters"].get("title", "")) if tools else ""
        forced = (body.get("tool_choice") or {}).get("function", {}).get("name") if isinstance(body.get("tool_choice"), dict) else None
        if forced == "submit_output":
            args = {"next_action": "close"} if title == "ClosureJudgment" else {}
            message = {"content": "", "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "submit_output", "arguments": json.dumps(args)}}]}
            finish = "tool_calls"
        else:
            message = {"content": CANDIDATE}
            finish = "stop"
        return 200, {"model": body.get("model"), "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", **message}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}

    def record(self, url: str, data: bytes) -> tuple[int, bytes, str]:
        body = json.loads(data or b"{}")
        with self._lock:
            self.calls.append({"url": url, "host": urlparse(url).hostname, "body": copy.deepcopy(body)})
        status, payload = self.reply(body)
        if body.get("stream"):
            return status, _sse(payload), "text/event-stream"
        return status, json.dumps(payload).encode(), "application/json"


def _sse(payload: dict[str, Any]) -> bytes:
    msg = payload["choices"][0]["message"]
    delta: dict[str, Any] = {"role": "assistant"}
    if msg.get("content"):
        delta["content"] = msg["content"]
    if msg.get("tool_calls"):
        delta["tool_calls"] = [{"index": i, **tc} for i, tc in enumerate(msg["tool_calls"])]
    chunk = {"model": payload.get("model"), "choices": [{"index": 0, "delta": delta, "finish_reason": payload["choices"][0]["finish_reason"]}]}
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()


WIRE = Wire()


class _UrllibResponse(io.BytesIO):
    def __init__(self, status: int, data: bytes, ctype: str) -> None:
        super().__init__(data)
        self.status = status
        self.headers = {"content-type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _urlopen(request, *args, **kwargs):
    url = request.full_url if hasattr(request, "full_url") else str(request)
    status, data, ctype = WIRE.record(url, request.data)
    return _UrllibResponse(status, data, ctype)


def _httpx_send(self, request, *args, **kwargs):
    status, data, ctype = WIRE.record(str(request.url), request.read())
    return httpx.Response(status, content=data, headers={"content-type": ctype}, request=request)


urllib.request.urlopen = _urlopen  # type: ignore[assignment]
httpx.Client.send = _httpx_send  # type: ignore[method-assign]

# ---------------------------------------------------------------- installed modules
from openminion.base.config.runtime.profile import build_runtime_config  # noqa: E402
from openminion.modules.llm.config import resolve_provider_identity_translation  # noqa: E402
from openminion.modules.llm.providers.factory import build_runtime_llm_handle  # noqa: E402
from openminion.modules.llm.providers.openai.request_compatibility import (  # noqa: E402
    OpenAIRequestCompatProfile,
    resolve_openai_request_compat,
)
from openminion.modules.llm.schemas import LLMRequest  # noqa: E402
from openminion.services.bootstrap.provider_setup import (  # noqa: E402
    ProviderSetupRequest,
    build_provider_setup,
    save_provider_setup,
)

c = Checks()
observed: dict[str, Any] = {}

SETUPS = {
    "gemini-preset": {"preset_id": "gemini", "agent_id": "gemini", "model": MODEL},
    "gemini-custom-name": {"preset_id": "custom-openai-compatible", "agent_id": "research-bot", "connection_id": "google-ai-studio", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": MODEL},
    "gemini-identity-from-base-url": {"preset_id": "gemini", "agent_id": "gemini", "model": MODEL, "drop_stored_identity": True},
    "gemini-model-behind-gateway": {"preset_id": "custom-openai-compatible", "agent_id": "gateway", "base_url": GATEWAY_BASE_URL, "model": MODEL},
    "openai": {"preset_id": "openai", "agent_id": "openai-agent", "model": "gpt-4.1-mini"},
    "minimax": {"preset_id": "minimax", "agent_id": "minimax-agent", "model": "MiniMax-M2.7"},
    "cortensor-portal": {"preset_id": "cortensor-portal", "agent_id": "cortensor-agent", "model": "oss-20b"},
}


def setup_result(label: str, preset_id: str, agent_id: str, model: str, base_url: str = "", connection_id: str = "", **_):
    home = WORK / "homes" / label
    return build_provider_setup(
        ProviderSetupRequest(
            preset_id=preset_id, agent_id=agent_id, model=model, base_url=base_url, connection_id=connection_id,
            config_path=str(home / ".openminion" / "agents.json"), home_root=home, data_root=home / ".openminion",
            env={"GEMINI_API_KEY": "fixture-key", "OPENAI_API_KEY": "fixture-key", "MINIMAX_API_KEY": "fixture-key", "CORTENSOR_API_KEY": "fixture-key", "OPENAI_COMPATIBLE_API_KEY": "fixture-key"},
        )
    ), home


def handle(label: str):
    spec = SETUPS[label]
    result, _home = setup_result(label, **spec)
    runtime_config = build_runtime_config(result.config, agent_id=spec["agent_id"])
    if spec.get("drop_stored_identity"):
        runtime_config.providers.openai.provider_identity = {}
    return build_runtime_llm_handle(runtime_config, logging.getLogger("probe.347"))


def schema_only_request() -> LLMRequest:
    return LLMRequest.model_validate({
        "messages": [{"role": "system", "content": "Primary system context."}, {"role": "user", "content": "Check order A-104"}],
        "tools": [{"name": "submit_output", "description": "return structured output", "input_schema": {"type": "object", "title": "ClosureJudgment"}}],
        "tool_choice": {"type": "function", "function": {"name": "submit_output"}},
    })


def text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    return "\n".join(p["text"].strip() for p in content if p.get("type") == "text")


def roles(body: dict[str, Any]) -> list[str]:
    return [m["role"] for m in body["messages"]]


def merged_equals_reference(gemini_body: dict[str, Any], reference_body: dict[str, Any]) -> tuple[bool, str]:
    ref = reference_body["messages"]
    system_texts = [text(m["content"]) for m in ref if m["role"] == "system"]
    expected = [{"role": "system", "content": "\n\n".join(system_texts)}, *[m for m in ref if m["role"] != "system"]]
    same_messages = gemini_body["messages"] == expected
    same_rest = {k: v for k, v in gemini_body.items() if k != "messages"} == {k: v for k, v in reference_body.items() if k != "messages"}
    return same_messages and same_rest and len(system_texts) >= 2, gemini_body["messages"][0]["content"] if gemini_body["messages"] else ""


# A1. profile resolution from the normalized identity ---------------------------
identity = resolve_provider_identity_translation("openai", model=MODEL, base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
profile = resolve_openai_request_compat(provider_identity=identity)
observed["A1_identity"] = {"identity": identity, "profile": profile.__dict__ if hasattr(profile, "__dict__") else repr(profile)}
import dataclasses  # noqa: E402

c.check("A1 Google endpoint identity -> gemini_openai_compat profile, only collapse_system_messages differs from default",
        dataclasses.asdict(profile) == {**dataclasses.asdict(OpenAIRequestCompatProfile()), "profile_id": "gemini_openai_compat", "collapse_system_messages": True},
        {"identity": identity, "profile_id": profile.profile_id})
for label, ident in (
    ("gemini model behind gateway", resolve_provider_identity_translation("openai", model=MODEL, base_url=GATEWAY_BASE_URL)),
    ("lookalike host", resolve_provider_identity_translation("openai", model=MODEL, base_url="https://generativelanguage.googleapis.com.example.test/v1")),
    ("openrouter gemini model", resolve_provider_identity_translation("openrouter", model="google/gemini-3.1-flash-lite")),
    ("no identity", None),
):
    p = resolve_openai_request_compat(provider_identity=ident)
    c.check(f"A1 {label} -> openai_default (unchanged)", p == OpenAIRequestCompatProfile(), p.profile_id)

# A2. schema-only request on every endpoint -------------------------------------
bodies: dict[str, dict[str, Any]] = {}
profiles: dict[str, str] = {}
urls: dict[str, str] = {}
for label in SETUPS:
    h = handle(label)
    try:
        mark = len(WIRE.calls)
        resp = h.client.call_sync(request=schema_only_request())
        bodies[label] = WIRE.calls[mark]["body"]
        urls[label] = WIRE.calls[mark]["url"]
        profiles[label] = resp.telemetry["normalization"]["request_compat_profile"]
    finally:
        h.close()
observed["A2_schema_only"] = {label: {"url": urls[label], "profile": profiles[label], "roles": roles(b), "messages": b["messages"]} for label, b in bodies.items()}
ref = bodies["gemini-model-behind-gateway"]
for label in ("gemini-preset", "gemini-custom-name", "gemini-identity-from-base-url"):
    ok, merged = merged_equals_reference(bodies[label], ref)
    c.check(f"A2 {label}: POST to {GEMINI_HOST}, profile gemini_openai_compat", urlparse(urls[label]).hostname == GEMINI_HOST and profiles[label] == "gemini_openai_compat", (urls[label], profiles[label]))
    c.check(f"A2 {label}: ONE system message = gateway's system messages joined in order, user turn and all other fields unchanged",
            ok and roles(bodies[label]) == ["system", "user"] and merged.startswith("Primary system context.\n\n" + CONTRACT), roles(bodies[label]))
expect = {
    "gemini-model-behind-gateway": ("openai_default", ["system", "system", "user"]),
    "openai": ("openai_default", ["system", "system", "user"]),
    "minimax": ("minimax_openai_compat", ["system", "user"]),
    "cortensor-portal": ("cortensor_portal", ["system", "system", "user"]),
}
for label, (prof, rl) in expect.items():
    msgs = bodies[label]["messages"]
    shape_ok = profiles[label] == prof and roles(bodies[label]) == rl
    if prof == "openai_default":
        shape_ok = shape_ok and msgs[0] == {"role": "system", "content": "Primary system context."} and text(msgs[1]["content"]).startswith(CONTRACT)
    c.check(f"A2 {label}: keeps profile {prof} and roles {rl}", shape_ok, {"profile": profiles[label], "roles": roles(bodies[label])})

# A3. ordinary tool-call transcript, complete and stream ------------------------
messages = [
    {"role": "system", "content": "Agent prompt: use lookup_order."},
    {"role": "user", "content": "Check order A-104"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "call-lookup", "name": "lookup_order", "arguments": {"order_id": "A-104"}}]},
    {"role": "tool", "content": TOOL_OUTPUT, "tool_call_id": "call-lookup", "tool_status": "success"},
    {"role": "system", "content": "Runtime note: answer in one sentence."},
]
tools = [{"name": "lookup_order", "description": "Look up one order.", "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}}}]
turns_expected = [
    {"role": "user", "content": "Check order A-104"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "call-lookup", "type": "function", "function": {"name": "lookup_order", "arguments": '{"order_id": "A-104"}'}}]},
    {"role": "tool", "content": TOOL_OUTPUT, "tool_call_id": "call-lookup"},
]
complete: dict[str, dict] = {}
stream: dict[str, dict] = {}
for label in ("gemini-model-behind-gateway", "gemini-preset"):
    h = handle(label)
    try:
        mark = len(WIRE.calls)
        out_text = h.client.call_sync(request=LLMRequest.model_validate({"messages": messages, "tools": tools, "tool_choice": "auto"})).output_text
        complete[label] = WIRE.calls[mark]["body"]
        mark = len(WIRE.calls)
        events = [e.type for e in h.client.stream(messages, tools, tool_choice="auto")]
        stream[label] = WIRE.calls[mark]["body"]
    finally:
        h.close()
    observed.setdefault("A3_transcript", {})[label] = {"complete_text": out_text, "stream_events": events, "complete_roles": roles(complete[label]), "stream_roles": roles(stream[label])}
gc, rc = complete["gemini-preset"], complete["gemini-model-behind-gateway"]
gs, rs = stream["gemini-preset"], stream["gemini-model-behind-gateway"]
ok_c, _ = merged_equals_reference(gc, rc)
ok_s, _ = merged_equals_reference(gs, rs)
observed["A3_transcript"]["payloads"] = {"gemini_complete": gc["messages"], "gateway_complete": rc["messages"], "gemini_stream": gs["messages"], "gateway_stream": rs["messages"]}
c.check("A3 gateway complete keeps several system messages (contract, agent prompt, later note)", roles(rc) == ["system", "system", "user", "assistant", "tool", "system"], roles(rc))
c.check("A3 Gemini complete: one leading system message (gateway's systems joined in order), user/assistant tool_calls/tool unchanged, tools/tool_choice unchanged",
        ok_c and gc["messages"][1:] == turns_expected and gc.get("tool_choice") == "auto" and [t["function"]["name"] for t in gc["tools"]] == ["lookup_order"], roles(gc))
c.check("A3 Gemini stream: one leading system message, turns unchanged, other stream fields equal the gateway's", ok_s and gs["messages"][1:] == turns_expected and gs.get("stream") is True, roles(gs))

# B. SDK turns ------------------------------------------------------------------
from openminion import Agent, APIRuntime, tool  # noqa: E402

fake = FakeProvider(FakeScript(tool_args={"lookup_order": {"order_id": "A-104"}}))  # in-process responder, server never started


def fake_responder(body):
    status, payload, _headers = fake.respond(body)
    return status, payload


def classify(body: dict[str, Any]) -> str:
    choice = body.get("tool_choice")
    forced = choice.get("function", {}).get("name") if isinstance(choice, dict) else ""
    joined = "\n".join(text(m.get("content") or "") if not isinstance(m.get("content"), type(None)) else "" for m in body.get("messages") or [])
    if forced == "submit_output":
        if "[JUDGE CONTEXT]" in joined or "purpose: judge" in joined:
            return "closure_judge"
        if "purpose: reflect" in joined:
            return "control_reflect"
        return "control_other"
    return "agent_loop"


sdk: dict[str, Any] = {}
for label in ("gemini-preset", "gemini-model-behind-gateway", "openai", "minimax"):
    spec = SETUPS[label]
    result, home = setup_result(f"sdk-{label}", **spec)
    save_provider_setup(result)
    os.environ.update({"OPENMINION_HOME": str(home), "OPENMINION_DATA_ROOT": str(home / ".openminion")})
    executed: list[str] = []

    @tool
    def lookup_order(order_id: str) -> str:
        """Return the current status and pickup code for one order."""
        executed.append(order_id)
        return f"order {order_id}: ready for pickup, pickup code ZEBRA-42"

    WIRE.responder = fake_responder
    mark = len(WIRE.calls)
    runtime = APIRuntime.from_config_path(str(home / ".openminion" / "agents.json"), home_root=home, data_root=home / ".openminion")
    err = ""
    res_text = ""
    try:
        with Agent(runtime=runtime, agent_id=spec["agent_id"], instructions="Use lookup_order, then answer in one sentence that quotes the pickup code.", tools=[lookup_order], forced_tools=["lookup_order"]) as agent:
            r = agent.run("Check order A-104", session_id=f"s347-{label}")
            res_text = str(getattr(r, "text", "") or "")
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        runtime.close()
        WIRE.responder = None
    calls = WIRE.calls[mark:]
    sdk[label] = {
        "hosts": sorted({cl["host"] for cl in calls}),
        "tool_executions": executed,
        "result_text": res_text,
        "error": err,
        "calls": [{"kind": classify(cl["body"]), "roles": roles(cl["body"]), "system_count": roles(cl["body"]).count("system"), "stream": bool(cl["body"].get("stream"))} for cl in calls],
    }
    judge = [cl["body"] for cl in calls if classify(cl["body"]) == "closure_judge"]
    sdk[label]["judge_system_messages"] = [[m["content"] for m in b["messages"] if m["role"] == "system"] for b in judge]
observed["B_sdk"] = sdk
g = sdk["gemini-preset"]
c.check("B gemini preset SDK turn: requests go to the Google host, tool ran, turn completed", g["hosts"] == [GEMINI_HOST] and g["tool_executions"] == ["A-104"] and not g["error"], {"hosts": g["hosts"], "tool": g["tool_executions"], "error": g["error"]})
c.check("B gemini preset SDK turn: EVERY request has exactly one system message, first", bool(g["calls"]) and all(cl["system_count"] == 1 and cl["roles"][0] == "system" for cl in g["calls"]), [cl["roles"] for cl in g["calls"]])
jsys = g["judge_system_messages"]
jtext = text(jsys[0][0]) if jsys and jsys[0] else ""
c.check("B gemini preset SDK turn: the closure judge's single system message holds [JUDGE CONTEXT] with the tool output ahead of the Tool-calling contract",
        len(jsys) >= 1 and len(jsys[0]) == 1 and "[JUDGE CONTEXT]" in jtext and TOOL_OUTPUT in jtext and CONTRACT in jtext and jtext.index(TOOL_OUTPUT) < jtext.rindex(CONTRACT),
        {"judge_calls": len(jsys), "system_messages_in_judge": len(jsys[0]) if jsys else 0})
for label, multi in (("gemini-model-behind-gateway", True), ("openai", True), ("minimax", False)):
    s = sdk[label]
    controls = [cl for cl in s["calls"] if cl["kind"] != "agent_loop"]
    if multi:
        ok = bool(controls) and all(cl["roles"][:3] == ["system", "system", "user"] for cl in controls)
        desc = "control/judge calls keep [system, system, user]"
    else:
        ok = bool(s["calls"]) and all(cl["system_count"] == 1 for cl in s["calls"])
        desc = "keeps its own single-system-message shape (minimax_openai_compat)"
    c.check(f"B {label} SDK turn: {desc}", ok and not s["error"], {"roles": [cl["roles"] for cl in s["calls"]], "error": s["error"]})

c.check("no outbound network connection was attempted past the mock", BLOCKED_CONNECTS == [], BLOCKED_CONNECTS)
result = {**info, "observed": observed, "checks": c.items, "all_pass": c.all_pass, "transport": "urllib.request.urlopen + httpx.Client.send replaced in-process; sockets blocked; no network"}
write_result(OUT, json.loads(json.dumps(result, default=str).replace(str(WORK).replace("\\", "\\\\"), "<WORK>")))
print("ALL PASS" if c.all_pass else "SOME CHECKS FAILED")
raise SystemExit(0 if c.all_pass else 1)
