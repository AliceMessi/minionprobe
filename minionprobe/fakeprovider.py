"""A tiny scripted OpenAI-compatible chat-completions server.

OpenMinion can talk to any OpenAI-compatible endpoint via the
``custom-openai-compatible`` setup preset. Pointing it at this server gives
MinionProbe a deterministic, offline, zero-cost model so we can:

* run pytest without network or API keys;
* inject provider faults at exact points of a turn (for example a 429 on the
  call that follows a tool execution);
* see exactly which model id the runtime asked for (agent routing checks).

Behaviour, per request:

* forced ``submit_output`` (OpenMinion's hidden control calls: memory reflect,
  freshness classifier, closure judge): returns a tool call whose arguments
  are generated from the tool's JSON schema, with a few semantic overrides
  (the judge closes the turn and repeats the last answer);
* agent-loop call with tools and no tool result yet: calls the first tool
  with schema-generated (or scripted) arguments;
* otherwise: returns ``FAKE-ANSWER(<model>): <last tool result or user text>``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Fault:
    """Return ``status`` instead of a normal answer when ``when`` matches.

    ``when`` is one of ``always``, ``after_tool`` (agent-loop call that already
    carries a tool result) or ``first_agent_call``. ``times`` limits how many
    requests get the fault (``0`` = unlimited).
    """

    status: int
    when: str = "always"
    times: int = 0
    message: str = "Rate limit reached for model fake-model. Please try again in 1s."
    hits: int = 0


@dataclass
class FakeScript:
    tool_args: dict[str, dict[str, Any]] = field(default_factory=dict)
    final_answer: str | None = None
    faults: list[Fault] = field(default_factory=list)


def _resolve(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    while isinstance(schema, dict) and "$ref" in schema:
        name = str(schema["$ref"]).split("/")[-1]
        schema = defs.get(name, {})
    return schema if isinstance(schema, dict) else {}


_SEMANTIC_DEFAULTS: dict[str, Any] = {
    "satisfied": True,
    "next_action": "close",
    "complete": True,
    "time_sensitive": False,
    "needs_live_data": False,
    "needs_sources": False,
    "needs_exact_date": False,
    "answer_mode": "local_only",
    "confidence": 0.9,
    "mutation_claimed": False,
    "items": [],
    "memory_use_refs": [],
}


def fill_from_schema(
    schema: dict[str, Any],
    defs: dict[str, Any] | None = None,
    key: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> Any:
    """Produce a minimal value that validates against a JSON schema."""
    defs = dict(defs or {})
    defs.update(schema.get("$defs", {}) or {})
    defs.update(schema.get("definitions", {}) or {})
    overrides = overrides or {}
    schema = _resolve(schema, defs)
    if key is not None and key in overrides:
        return overrides[key]
    if key is not None and key in _SEMANTIC_DEFAULTS:
        default = _SEMANTIC_DEFAULTS[key]
        enum = schema.get("enum")
        if not enum or default in enum:
            return default
    for combinator in ("anyOf", "oneOf", "allOf"):
        options = schema.get(combinator)
        if options:
            non_null = [o for o in options if _resolve(o, defs).get("type") != "null"]
            return fill_from_schema((non_null or options)[0], defs, key, overrides)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {}) or {}
        required = schema.get("required", list(props))
        return {
            name: fill_from_schema(props[name], defs, name, overrides)
            for name in required
            if name in props
        }
    if kind == "array":
        count = int(schema.get("minItems", 0) or 0)
        return [fill_from_schema(schema.get("items", {}), defs, None, overrides) for _ in range(count)]
    if kind == "integer":
        return int(schema.get("minimum", 0) or 0)
    if kind == "number":
        return float(schema.get("minimum", 0) or 0)
    if kind == "boolean":
        return False
    return "fake"


def _last(messages: list[dict[str, Any]], role: str) -> str:
    for message in reversed(messages):
        if message.get("role") == role:
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            return str(content or "")
    return ""


_ACTION_SUMMARY = re.compile(r"action_summary: (FAKE-ANSWER[^\n]*)")


def _action_summary(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        match = _ACTION_SUMMARY.search(str(message.get("content") or ""))
        if match:
            return match.group(1).strip()
    return ""


class FakeProvider:
    def __init__(self, script: FakeScript | None = None, host: str = "127.0.0.1", port: int = 0) -> None:
        self.script = script or FakeScript()
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                return

            def do_GET(self) -> None:  # /v1/models
                self._send(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    body = {}
                status, payload, headers = provider.respond(body)
                self._send(status, payload, headers)

            def _send(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------------
    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> "FakeProvider":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "FakeProvider":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- behaviour --------------------------------------------------------------
    def respond(self, body: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
        messages = body.get("messages") or []
        tools = [t.get("function", {}) for t in (body.get("tools") or []) if isinstance(t, dict)]
        tool_names = [str(t.get("name", "")) for t in tools]
        choice = body.get("tool_choice")
        forced = choice.get("function", {}).get("name") if isinstance(choice, dict) else None
        has_tool_result = any(m.get("role") == "tool" for m in messages if isinstance(m, dict))
        is_control = forced == "submit_output" or tool_names == ["submit_output"]
        with self._lock:
            self.requests.append(
                {
                    "model": body.get("model"),
                    "tools": tool_names,
                    "forced": forced,
                    "control": is_control,
                    "has_tool_result": has_tool_result,
                    "ts": time.time(),
                }
            )
            agent_calls = sum(1 for r in self.requests if not r["control"])
            fault = self._match_fault(is_control, has_tool_result, agent_calls)
        model = str(body.get("model") or "fake-model")
        if fault is not None:
            return (
                fault.status,
                {"error": {"message": fault.message, "type": "tokens", "code": "rate_limit_exceeded" if fault.status == 429 else "server_error"}},
                {"retry-after": "1"} if fault.status == 429 else {},
            )
        if is_control:
            spec = next((t for t in tools if t.get("name") == "submit_output"), {})
            overrides: dict[str, Any] = {}
            # behave like a well-grounded judge: repeat the agent loop's answer
            # (OpenMinion passes it as "action_summary" in the judge context);
            # otherwise echo the user's request so every turn stays distinct
            summary = _action_summary(messages)
            answer = self.script.final_answer or summary or f"FAKE-ANSWER({model}): {_last(messages, 'user')[-200:]}"
            overrides["final_answer"] = answer
            args = fill_from_schema(spec.get("parameters") or {}, overrides=overrides)
            return 200, self._tool_call_completion(model, "submit_output", args), {}
        user_tools = [t for t in tools if t.get("name") != "submit_output"]
        if user_tools and not has_tool_result:
            spec = user_tools[0]
            name = str(spec.get("name"))
            args = self.script.tool_args.get(name) or fill_from_schema(spec.get("parameters") or {})
            return 200, self._tool_call_completion(model, name, args), {}
        source = _last(messages, "tool") if has_tool_result else _last(messages, "user")
        text = self.script.final_answer or f"FAKE-ANSWER({model}): {source[:300]}"
        return 200, self._text_completion(model, text), {}

    def _match_fault(self, is_control: bool, has_tool_result: bool, agent_calls: int) -> Fault | None:
        for fault in self.script.faults:
            if fault.times and fault.hits >= fault.times:
                continue
            hit = (
                fault.when == "always"
                or (fault.when == "after_tool" and has_tool_result and not is_control)
                or (fault.when == "first_agent_call" and not is_control and agent_calls == 1)
            )
            if hit:
                fault.hits += 1
                return fault
        return None

    @staticmethod
    def _usage() -> dict[str, int]:
        return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def _tool_call_completion(self, model: str, name: str, args: Any) -> dict[str, Any]:
        call_id = f"call_{len(self.requests)}"
        return {
            "id": f"fake-{len(self.requests)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                        ],
                    },
                }
            ],
            "usage": self._usage(),
        }

    def _text_completion(self, model: str, text: str) -> dict[str, Any]:
        return {
            "id": f"fake-{len(self.requests)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": self._usage(),
        }
