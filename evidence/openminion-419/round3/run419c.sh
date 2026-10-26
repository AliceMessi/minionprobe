#!/bin/bash
set -u
DEV="a213dcd247c5b48fa8ee823a9f169236a54ac231"
F="tests/api/test_text_answer_freshness.py tests/api/test_freshness_continuation.py tests/brain/test_freshness_entry_retention.py"
F2="tests/brain/test_turn_evidence_persistence.py"
R="tests/brain/test_freshness_routing.py tests/brain/test_freshness_enforcement.py tests/brain/test_unified_turn_entry.py tests/services/brain/test_current_date_evidence_grounding.py tests/brain/test_runner_context.py tests/test_cted_regression.py tests/brain/test_integration_brain_v1.py tests/modules/brain/loop/tools/test_engine_characterization.py tests/api/test_provider_failure.py tests/brain/modes/test_act_adaptive.py tests/modules/brain/loop/test_adaptive_characterization.py tests/brain/test_post_action.py"
RM="tests/brain tests/api tests/modules/brain"
RMARK="not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow"
PROBE="/tmp/ev/probe_text_answer_freshness.py"
P=.venv/bin/python; DEV=${DEV:-a41487ab1b51f7fd526ff29cd526f0b98dc01fc7}
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($P -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|sophiagraph|typer) ' | tr -s ' ' | tr '\n' ';')"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
mkdir -p /tmp/ev
cat > /tmp/ev/probe_text_answer_freshness.py <<'PYEOF_X'
"""Is a text entry answer checked against the turn's freshness obligations?

Follow-up to openminion #395. An SDK `Agent` with `@tool lookup_order`
(min_scope="READ_ONLY") runs "Check order A-104" against a scripted
OpenAI-compatible provider on 127.0.0.1 (default `openai` provider, no keys).
The freshness classifier answers time_sensitive, needs_live_data, needs_sources
and needs_exact_date, so the turn's typed obligations require live, sourced,
exact-date evidence; `lookup_order` returns no dated field.

Scenarios (one input changed at a time):
  T0  the first entry step answers in text (no tool call); no forced tools
  T1  forced `lookup_order`; the judge closes, the closure freshness guard turns
      the close into a continue, and the continuation entry answers in text
      (the #328 trace B / #395 scenario B)
  C   control: as T1, but the continuation entry calls `lookup_order` again
      and the judge closes again (the tool path, trace E)

Read-only observers record, at every `_apply_entry_freshness` call, the typed
obligations after it; every call of `verify_freshness_answer` from the modules
that import it (the closure evaluator's guard, `final_close_message`, the
verification policy), with its caller and result; and each `brain.entry`
decision (route and reason code).

Run from an openminion checkout: PYTHONPATH=src:. python probe_text_answer_freshness.py
(PYTHONPATH="src;." on Windows). Only loopback connections are allowed.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path[:] = [os.path.abspath(entry) if entry else entry for entry in sys.path]

CODE = "ZEBRA-42"
FINALIZATION = (
    '<finalization_status>{"status":"final_answer","reasoning":"The order status '
    'and pickup code were retrieved with lookup_order."}</finalization_status>'
)
BLOCKED: list[str] = []
_connect = socket.socket.connect


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    host = address[0] if isinstance(address, tuple) and address else address
    if self.family in (socket.AF_INET, socket.AF_INET6) and str(host) not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        BLOCKED.append(str(address))
        raise ConnectionRefusedError(f"probe blocks non-loopback connect to {address!r}")
    return _connect(self, address)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign]


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
    call = {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    return _completion(
        {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
    )


def _text(content: str) -> dict[str, Any]:
    return _completion({"role": "assistant", "content": content}, "stop")


def _all_text(body: dict[str, Any]) -> str:
    parts = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        parts.append(str(content or ""))
    return "\n".join(parts)


class Script:
    def __init__(self, *, first_entry: str, continuation: str) -> None:
        self.first_entry = first_entry  # "text" | "tool"
        self.continuation = continuation  # "text" | "tool"
        self.kinds: list[str] = []
        self.lock = threading.Lock()

    def respond(self, body: dict[str, Any]) -> dict[str, Any]:
        text = _all_text(body)
        choice = body.get("tool_choice")
        forced = (
            str((choice.get("function") or {}).get("name", ""))
            if isinstance(choice, dict)
            else ""
        )
        after_tool = any(m.get("role") == "tool" for m in body.get("messages") or [])
        if forced == "submit_output":
            if "purpose: judge" in text or "[JUDGE CONTEXT]" in text:
                kind = "judge"
            elif "purpose: reflect" in text:
                kind = "reflect"
            elif "freshness-routing classifier" in text:
                kind = "freshness"
            else:
                kind = "other_structured"
        elif "purpose: decide" in text:
            kind = "entry_after_tool" if after_tool else "entry"
        else:
            kind = "act_loop" if after_tool else "other_text"
        with self.lock:
            index = len(self.kinds)
            self.kinds.append(kind)
        if kind == "reflect":
            return _tool_call(
                "submit_output",
                {"items": [{"title": "Check order A-104", "normalized_key": "task:a104",
                            "kind": "task", "content": "Check order A-104"}]},
                f"call-reflect-{index}",
            )
        if kind == "freshness":
            return _tool_call(
                "submit_output",
                {"intent": "order_status_inquiry", "domain": "shopping",
                 "time_sensitive": True, "needs_live_data": True, "needs_sources": True,
                 "needs_exact_date": True, "answer_mode": "browse_then_answer",
                 "reason": "Order status is live data.", "confidence": 1},
                f"call-freshness-{index}",
            )
        if kind == "judge":
            return _tool_call(
                "submit_output",
                {"final_answer": f"[judge] Order A-104 is ready, code {CODE}.",
                 "satisfied": True, "next_action": "close"},
                f"call-judge-{index}",
            )
        if kind == "entry":
            if self.first_entry == "text":
                return _text(f"[entry text] Order A-104 is ready, code {CODE}.")
            return _tool_call("lookup_order", {"order_id": "A-104"}, f"call-lookup-{index}")
        if kind == "entry_after_tool":
            if self.continuation == "tool" and self.kinds.count("entry_after_tool") == 1:
                return _tool_call("lookup_order", {"order_id": "A-104"}, f"call-lookup-{index}")
            return _text(f"[continuation text] Order A-104 is ready, code {CODE}.")
        if kind == "act_loop":
            return _text(f"[loop] Order A-104 is ready, code {CODE}.\n\n{FINALIZATION}")
        if kind == "other_structured":
            return _tool_call("submit_output", {}, f"call-other-{index}")
        return _text("ok")


OBSERVED: list[dict[str, Any]] = []


def _install_observers() -> None:
    from openminion.modules.brain.diagnostics.events import CanonicalEventLogger
    from openminion.modules.brain.execution.closure import base as closure_base
    from openminion.modules.brain.execution.runtime.closure import evaluator
    from openminion.modules.brain.loop import orchestration
    from openminion.modules.brain.runtime.verification import policy

    real_entry = orchestration._apply_entry_freshness

    def entry(**kwargs: Any) -> Any:
        result = real_entry(**kwargs)
        state = kwargs["state"]
        obligations = state.freshness_obligations
        OBSERVED.append(
            {
                "at": "entry freshness",
                "user_input": kwargs.get("user_input"),
                "entry_response": "tool call" if getattr(kwargs["response"], "tool_calls", None) else "text",
                "obligations after": None if obligations is None else {
                    "require_live_data": obligations.require_live_data,
                    "require_sources": obligations.require_sources,
                    "require_exact_date": obligations.require_exact_date,
                },
                "classifier_mode": getattr(state.freshness_diagnostics, "classifier_mode", None),
            }
        )
        return result

    orchestration._apply_entry_freshness = entry

    for module in (evaluator, closure_base, policy):
        real = module.verify_freshness_answer

        def verify(*args: Any, _real: Any = real, _module: Any = module, **kwargs: Any) -> Any:
            reasons = _real(*args, **kwargs)
            OBSERVED.append(
                {
                    "at": "verify_freshness_answer",
                    "module": _module.__name__.removeprefix("openminion.modules.brain."),
                    "caller": sys._getframe(1).f_code.co_name,
                    "result": reasons or "pass",
                }
            )
            return reasons

        module.verify_freshness_answer = verify

    real_emit = CanonicalEventLogger.emit

    def emit(self: Any, event_type: str, payload: dict[str, Any], *a: Any, **k: Any) -> Any:
        if event_type == "brain.entry":
            OBSERVED.append(
                {"at": "brain.entry", "route": payload.get("route"),
                 "reason_code": payload.get("reason_code")}
            )
        return real_emit(self, event_type, payload, *a, **k)

    CanonicalEventLogger.emit = emit  # type: ignore[method-assign]


def run_scenario(name: str, script: Script, forced: list[str]) -> None:
    from openminion import Agent, APIRuntime, tool
    from openminion.base.config import OpenMinionConfig, save_config
    from tests._csc_fixtures import _csc_install_default_agent

    OBSERVED.clear()

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

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tmp = Path(tempfile.mkdtemp(prefix=f"omtext-{name}-"))
    os.environ["OPENMINION_HOME"] = str(tmp)
    os.environ["OPENMINION_DATA_ROOT"] = str(tmp / "data")
    os.environ["PROBE_PROVIDER_KEY"] = "local-dummy-key"
    config = OpenMinionConfig()
    _csc_install_default_agent(config, provider="openai")
    config.providers.openai.api_key_env = "PROBE_PROVIDER_KEY"
    config.providers.openai.base_url = f"http://127.0.0.1:{server.server_port}/v1"
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(tmp / "state.db")
    save_config(config, str(tmp / "agents.json"))
    calls: list[str] = []

    @tool(min_scope="READ_ONLY")
    def lookup_order(order_id: str) -> str:
        """Return the current status and pickup code for one order."""
        calls.append(order_id)
        return f"order {order_id}: ready for pickup, pickup code {CODE}"

    previous = os.getcwd()
    os.chdir(tmp)
    runtime = APIRuntime.from_config_path(str(tmp / "agents.json"), home_root=tmp, data_root=tmp / "data")
    try:
        with Agent(
            runtime=runtime,
            instructions="Use lookup_order, then answer in one sentence that quotes the pickup code.",
            tools=[lookup_order],
            forced_tools=forced,
        ) as agent:
            result = agent.run("Check order A-104", session_id=f"text-{name}", timeout_seconds=120)
    finally:
        try:
            runtime.close()
        finally:
            os.chdir(previous)
            server.shutdown()
            server.server_close()
            thread.join()
    print(f"\n== {name}")
    for item in OBSERVED:
        print("  " + json.dumps(item))
    print(f"tool executions: {calls}")
    print(f"result.text: {result.text!r}")


def main() -> int:
    import logging

    import openminion

    logging.disable(logging.WARNING)
    checkout = Path(openminion.__file__).resolve().parents[2]
    commit = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--short=8", "HEAD"],
                            capture_output=True, text=True, check=False).stdout.strip()
    print(f"openminion {openminion.__version__}, checkout {commit or 'unknown'}, "
          f"python {sys.version.split()[0]} {sys.platform}")
    _install_observers()
    run_scenario("T0_first_entry_text", Script(first_entry="text", continuation="text"), [])
    run_scenario("T1_continuation_text", Script(first_entry="tool", continuation="text"), ["lookup_order"])
    run_scenario("C_control_continuation_tool", Script(first_entry="tool", continuation="tool"), ["lookup_order"])
    print(f"\nnon-loopback connections attempted: {len(BLOCKED)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PYEOF_X
[ -n "${PROBE:-}" ] && sha256sum /tmp/ev/*
pyt() { $P -m pytest -q -p no:cacheprovider -rfE --tb=line "$@" 2>&1 | grep -E "^FAILED|^ERROR|passed|failed|error" | sed -E 's/^(.{240}).*/\1 .../'; }
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; sec "dev sources from here on: git checkout $DEV -- src ($(git status --short | grep -v '^?? .deps' | wc -l) paths differ)"; fi
  sec "[$tree] pytest <focused>"; pyt $F
  if [ -n "${F2:-}" ]; then sec "[$tree] pytest <persistence>"; pyt $F2; fi
  if [ -n "${R:-}" ]; then sec "[$tree] pytest <related>"; pyt $R; fi
  if [ -n "${RM:-}" ]; then CE=""; [ $tree = dev ] && CE="--continue-on-collection-errors"; sec "[$tree] pytest -m \"$RMARK\" $CE $RM"; pyt -m "$RMARK" $CE $RM; fi
  if [ -n "${PGX:-}" ]; then sec "[$tree] pytest -m postgres <postgres>"; pyt -m postgres $PGX; fi
  if [ -n "${PROBE:-}" ]; then sec "[$tree] the issue's probe: PYTHONPATH=src:. python $(basename $PROBE)"; PYTHONPATH=src:. $P $PROBE 2>&1 | grep -v "^20[0-9][0-9]-"; fi
  if [ -n "${PROBE2:-}" ]; then sec "[$tree] PYTHONPATH=src:. python $(basename $PROBE2)"; PYTHONPATH=src:. $P $PROBE2 2>&1 | grep -v "^20[0-9][0-9]-"; fi
done
git checkout -q HEAD -- src; sec "back to the branch head: $(git status --short | grep -v '^?? .deps' | wc -l) paths differ"
sec "ruff check ."; .venv/bin/ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make workflow-check"; make workflow-check 2>&1 | tail -4
sec "make lint"; make lint 2>&1 | tail -40; echo "make lint exit: ${PIPESTATUS[0]}"
sec "make test-critical"; make test-critical 2>&1 | tail -3
