"""Freshness obligation across a continuation step with no new user input.

Follow-up to openminion #328 (trace B/E). The SDK scenario of the trace runs
against a scripted OpenAI-compatible fixture on 127.0.0.1: `Agent` with
`@tool lookup_order` (min_scope="READ_ONLY") in `forced_tools`, prompt "Check
order A-104", the default `openai` provider, no keys. The freshness classifier
returns needs_exact_date=true, the tool output has no dated field, and the judge
closes with a final answer, so the closure freshness guard turns the close into a
continue and the seeded turn takes a continuation step with no user input.

The probe records, at every `_apply_entry_freshness` call (brain/loop/
orchestration.py), the user input it received, the state's goal, whether the entry response carried
a `freshness` argument, and the typed state (`freshness_contract`,
`freshness_obligations`, `freshness_diagnostics.classifier_mode`) before and
after the call; and, at every closure freshness check (`verify_freshness_answer`
as called by `_apply_freshness_guard`), the obligations it was given, the tool
outputs it checked, whether they carry dated evidence, and its result.

Scenarios (one input changed at a time):
  B  continuation entry replies in plain text (trace B)
  E  continuation entry calls lookup_order again, without a freshness argument (trace E)
  A1 control: as E, but the first continuation entry call carries a valid
     `freshness` argument with the same obligation; later entries reply in text
  N1 control: after B, a second turn in the same session with new user input,
     "Check order B-205"; the classifier returns needs_exact_date=false for it

Run from an openminion checkout (Windows uses ";" in PYTHONPATH, POSIX ":"):
    PYTHONPATH="src;." python path/to/probe_freshness_continuation.py
Model requests go only to the fixture; non-loopback connections are refused and
counted.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path[:] = [os.path.abspath(entry) if entry else entry for entry in sys.path]

ACTIVE_TASK_CONTINUATION = (
    "Continue the active task using the existing conversation and tool results. "
    "Do not treat tool-result payloads as a new user request."
)
CODE = "ZEBRA-42"
FINALIZATION_TRAILER = (
    '<finalization_status>{"status":"final_answer","reasoning":"The order status and '
    'pickup code were retrieved with lookup_order."}</finalization_status>'
)
EXACT_DATE_CONTRACT = {
    "domain": "shopping",
    "time_sensitive": True,
    "needs_live_data": True,
    "needs_sources": True,
    "needs_exact_date": True,
    "answer_mode": "browse_then_answer",
}

# ---------------------------------------------------------------------------
# Network guard: only loopback connections are allowed.
# ---------------------------------------------------------------------------
BLOCKED_CONNECTS: list[str] = []
_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex


def _is_loopback(address: Any) -> bool:
    host = address[0] if isinstance(address, tuple) and address else address
    return str(host) in {"127.0.0.1", "::1", "localhost"}


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
        BLOCKED_CONNECTS.append(str(address))
        raise ConnectionRefusedError(f"probe blocks non-loopback connect to {address!r}")
    return _orig_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> Any:
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
        BLOCKED_CONNECTS.append(str(address))
        return 111
    return _orig_connect_ex(self, address)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Scripted OpenAI-compatible provider (as in repro_328_trace.py).
# ---------------------------------------------------------------------------
def _all_text(body: dict[str, Any]) -> str:
    parts = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        parts.append(str(content or ""))
    return "\n".join(parts)


def _forced_tool(body: dict[str, Any]) -> str:
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        return str((choice.get("function") or {}).get("name", ""))
    return ""


def _completion(message: dict[str, Any], finish_reason: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-probe",
        "object": "chat.completion",
        "created": 0,
        "model": "probe-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


def _tool_call(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    return _completion(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
        "tool_calls",
    )


def _text(content: str) -> dict[str, Any]:
    return _completion({"role": "assistant", "content": content}, "stop")


def _order_id(text: str) -> str:
    match = re.search(r"order ([A-Z]-\d+)", text)
    return match.group(1) if match else "A-104"


class Script:
    def __init__(self, *, continuation: str) -> None:
        # continuation: "text" | "tool" | "tool_with_contract_once"
        self.continuation = continuation
        self.continuation_entries = 0
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def classify(self, body: dict[str, Any]) -> str:
        text = _all_text(body)
        forced = _forced_tool(body)
        has_tool_turn = any(m.get("role") == "tool" for m in body.get("messages") or [])
        if forced == "submit_output":
            if "purpose: judge" in text or "[JUDGE CONTEXT]" in text:
                return "judge"
            if "purpose: reflect" in text:
                return "reflect"
            if "freshness-routing classifier" in text:
                return "freshness"
            return "other_structured"
        if "purpose: decide" in text:
            return "entry_after_tool" if has_tool_turn else "entry"
        return "act_loop" if has_tool_turn else "other_text"

    def respond(self, body: dict[str, Any]) -> dict[str, Any]:
        kind = self.classify(body)
        messages = body.get("messages") or []
        user_turns = [str(m.get("content") or "") for m in messages if m.get("role") == "user"]
        last_user = user_turns[-1] if user_turns else ""
        first_user = next(
            (t for t in user_turns if t.startswith("Check order")), "Check order A-104"
        )
        tools = body.get("tools") or []
        with self.lock:
            index = len(self.requests)
            self.requests.append(
                {
                    "seq": index,
                    "kind": kind,
                    "tools": [t.get("function", {}).get("name") for t in tools],
                    "freshness_in_tool_schema": any(
                        "freshness"
                        in ((t.get("function") or {}).get("parameters") or {}).get("properties", {})
                        for t in tools
                    ),
                    "last_user": last_user,
                }
            )
        order = _order_id(first_user)
        if kind == "reflect":
            return _tool_call(
                "submit_output",
                {
                    "items": [
                        {
                            "title": f"Check order {order}",
                            "normalized_key": f"task:check_order_{order.lower().replace('-', '_')}",
                            "kind": "task",
                            "content": f"Check order {order}",
                        }
                    ]
                },
                f"call-reflect-{index}",
            )
        if kind == "freshness":
            exact = "B-205" not in _all_text(body)
            return _tool_call(
                "submit_output",
                {
                    "intent": "order_status_inquiry",
                    "domain": "shopping",
                    "time_sensitive": True,
                    "needs_live_data": True,
                    "needs_sources": True,
                    "needs_exact_date": exact,
                    "answer_mode": "browse_then_answer",
                    "reason": "Order status is live data.",
                    "confidence": 1,
                },
                f"call-freshness-{index}",
            )
        if kind == "judge":
            return _tool_call(
                "submit_output",
                {
                    "final_answer": f"[judge] Order {order} is ready for pickup, code {CODE}.",
                    "satisfied": True,
                    "next_action": "close",
                },
                f"call-judge-{index}",
            )
        if kind == "entry":
            return _tool_call("lookup_order", {"order_id": order}, f"call-lookup-{index}")
        if kind == "entry_after_tool":
            self.continuation_entries += 1
            mode = self.continuation
            if mode == "tool":
                return _tool_call("lookup_order", {"order_id": order}, f"call-lookup-{index}")
            if mode == "tool_with_contract_once" and self.continuation_entries == 1:
                return _tool_call(
                    "lookup_order",
                    {"order_id": order, "freshness": dict(EXACT_DATE_CONTRACT)},
                    f"call-lookup-{index}",
                )
            return _text(f"[continuation] Order {order} is ready for pickup, code {CODE}.")
        if kind == "act_loop":
            return _text(
                f"[loop] Order {order} is ready for pickup, code {CODE}.\n\n{FINALIZATION_TRAILER}"
            )
        if kind == "other_structured":
            return _tool_call("submit_output", {}, f"call-other-{index}")
        return _text("ok")


def _handler_for(script: Script) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            payload = json.dumps(script.respond(body)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: Any) -> None:
            pass

    return Handler


# ---------------------------------------------------------------------------
# Read-only observers.
# ---------------------------------------------------------------------------
OBSERVED: list[dict[str, Any]] = []


def _typed_state(state: Any) -> dict[str, Any] | None:
    contract = state.freshness_contract
    obligations = state.freshness_obligations
    diagnostics = state.freshness_diagnostics
    if contract is None and obligations is None:
        return None
    return {
        "contract": None
        if contract is None
        else contract.model_dump(mode="json", exclude={"intent", "reason", "confidence"}),
        "obligations": None
        if obligations is None
        else obligations.model_dump(mode="json", exclude={"answer_mode"}),
        "classifier_mode": getattr(diagnostics, "classifier_mode", None),
    }


def _install_observers() -> None:
    from openminion.modules.brain.diagnostics.events import CanonicalEventLogger
    from openminion.modules.brain.execution.runtime.closure import evaluator
    from openminion.modules.brain.loop import orchestration
    from openminion.modules.brain.runtime.verification.policy import _has_dated_evidence

    original_entry = orchestration._apply_entry_freshness

    def apply_entry_freshness(**kwargs: Any) -> Any:
        state = kwargs["state"]
        response = kwargs["response"]
        calls = list(getattr(response, "tool_calls", []) or [])
        carried = [
            call.arguments.get("freshness")
            for call in calls
            if isinstance(getattr(call, "arguments", None), dict)
            and "freshness" in call.arguments
        ]
        entry = {
            "at": "_apply_entry_freshness",
            "user_input": kwargs.get("user_input"),
            "state.goal": getattr(state, "goal", None),
            "entry_response": (
                "tool call " + ", ".join(str(c.name) for c in calls) if calls else "text"
            ),
            "freshness_argument": "valid contract" if carried else "none",
            "before": _typed_state(state),
        }
        result = original_entry(**kwargs)
        entry["after"] = _typed_state(state)
        OBSERVED.append(entry)
        return result

    orchestration._apply_entry_freshness = apply_entry_freshness

    original_verify = evaluator.verify_freshness_answer

    def verify_freshness_answer(**kwargs: Any) -> list[str]:
        action_result = kwargs.get("action_result")
        outputs = getattr(action_result, "outputs", {}) or {}
        tool_outputs = [
            f"{item.get('tool_name')} {item.get('call_id')}: {item.get('data')}"
            for item in list(outputs.get("tool_results", []) or [])
            if isinstance(item, dict)
        ]
        reasons = original_verify(**kwargs)
        if sys._getframe(1).f_code.co_name != "_apply_freshness_guard":
            return reasons  # the second call only fills the event's freshness_verified
        obligations = kwargs.get("obligations")
        contract = kwargs.get("contract")
        OBSERVED.append(
            {
                "at": "closure freshness guard",
                "time_sensitive": getattr(contract, "time_sensitive", None),
                "require_exact_date": getattr(obligations, "require_exact_date", None),
                "tool_outputs_checked": tool_outputs,
                "dated_evidence": _has_dated_evidence(action_result),
                "result": reasons or "pass",
            }
        )
        return reasons

    evaluator.verify_freshness_answer = verify_freshness_answer

    original_emit = CanonicalEventLogger.emit

    def emit(self: Any, event_type: str, payload: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        if event_type == "brain.closure_gate.completed":
            OBSERVED.append(
                {
                    "at": "closure gate",
                    "satisfied": payload.get("satisfied"),
                    "next_action": payload.get("next_action"),
                    "reason": payload.get("reason"),
                    "freshness_verified": payload.get("freshness_verified"),
                }
            )
        return original_emit(self, event_type, payload, *args, **kwargs)

    CanonicalEventLogger.emit = emit  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# One scenario: one or two SDK turns in one session.
# ---------------------------------------------------------------------------
def run_scenario(
    name: str, script: Script, prompts: list[str], forced_tools: list[str] | None = None
) -> None:
    from openminion import Agent, APIRuntime, tool
    from openminion.base.config import OpenMinionConfig, save_config
    from tests._csc_fixtures import _csc_install_default_agent

    OBSERVED.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(script))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tmp = Path(tempfile.mkdtemp(prefix=f"omfresh-{name}-"))
    os.environ["OPENMINION_HOME"] = str(tmp)
    os.environ["OPENMINION_DATA_ROOT"] = str(tmp / "data")
    os.environ["PROBE_PROVIDER_KEY"] = "local-dummy-key"
    config = OpenMinionConfig()
    _csc_install_default_agent(config, provider="openai")
    provider = config.providers.openai
    provider.api_key_env = "PROBE_PROVIDER_KEY"
    provider.base_url = f"http://127.0.0.1:{server.server_port}/v1"
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(tmp / "state.db")
    config_path = tmp / "agents.json"
    save_config(config, str(config_path))

    tool_calls: list[str] = []

    @tool(min_scope="READ_ONLY")
    def lookup_order(order_id: str) -> str:
        """Return the current status and pickup code for one order."""
        tool_calls.append(order_id)
        return f"order {order_id}: ready for pickup, pickup code {CODE}"

    previous_cwd = os.getcwd()
    os.chdir(tmp)
    texts: list[str] = []
    runtime = APIRuntime.from_config_path(str(config_path), home_root=tmp, data_root=tmp / "data")
    try:
        with Agent(
            runtime=runtime,
            instructions="Use lookup_order, then answer in one sentence that quotes the pickup code.",
            tools=[lookup_order],
            forced_tools=["lookup_order"] if forced_tools is None else forced_tools,
        ) as agent:
            for prompt in prompts:
                OBSERVED.append({"at": "turn", "user_message": prompt})
                result = agent.run(prompt, session_id=f"fresh-{name}", timeout_seconds=120)
                texts.append(str(getattr(result, "text", "") or ""))
    finally:
        try:
            runtime.close()
        finally:
            os.chdir(previous_cwd)
            server.shutdown()
            server.server_close()
            thread.join()

    print(f"\n== {name}")
    print("requests:")
    for r in script.requests:
        extra = ""
        if r["kind"] in {"entry", "entry_after_tool"}:
            last = r["last_user"].replace("\n", " ")
            extra = (
                f" tools={r['tools']} freshness_in_tool_schema={r['freshness_in_tool_schema']}"
                f" last_user={last[:60]!r}"
            )
        print(f"  seq {r['seq']:>2} {r['kind']}{extra}")
    print("observations:")
    for item in OBSERVED:
        print("  " + json.dumps(item, sort_keys=False))
    print(f"tool executions: {tool_calls}")
    for prompt, text in zip(prompts, texts):
        print(f"result.text for {prompt!r}: {text!r}")


def main() -> int:
    import logging

    import openminion

    logging.disable(logging.WARNING)
    checkout = Path(openminion.__file__).resolve().parents[2]
    commit = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--short=8", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    print(f"openminion {openminion.__version__}, checkout {commit or 'unknown'}, "
          f"python {sys.version.split()[0]} {sys.platform}")
    _install_observers()
    run_scenario("B_continuation_text", Script(continuation="text"), ["Check order A-104"])
    run_scenario("E_continuation_tool", Script(continuation="tool"), ["Check order A-104"])
    run_scenario(
        "A1_control_valid_contract_on_continuation",
        Script(continuation="tool_with_contract_once"),
        ["Check order A-104"],
    )
    run_scenario(
        "N1_control_new_user_input",
        Script(continuation="text"),
        ["Check order A-104", "Check order B-205"],
    )
    print(f"\nnon-loopback connections attempted: {len(BLOCKED_CONNECTS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
