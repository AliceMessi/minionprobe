"""Helpers for the #419 probes (installed package only, no repo test fixtures).

- A scripted OpenAI-compatible provider served by ThreadingHTTPServer on
  127.0.0.1. The SDK's `openai` provider talks to it with a dummy key
  (base_url http://127.0.0.1:<port>/v1). A socket guard refuses any
  non-loopback connection and counts the attempts.
- `run_scenario`: a real `APIRuntime` + SDK `Agent(tools=[lookup_order])`
  over that provider, one or more turns, with read-only observers.
- Working-state readers through the installed owners: the brain's SQLite
  session store (`SessctlAdapter`, what `APIRuntime` uses) and the local
  session store (`LocalSessionStore`, what `brainctl` uses in local mode),
  each through `openminion.modules.brain.state.load_or_init_state`, plus raw
  read-only SQL / JSON reads of what is stored.

The same helpers run on 0.1.12 (the negative control): names that only
0.1.13 has are looked up with getattr and reported as absent.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

CODE = "ZEBRA-42"
DATED_AT = "2026-10-27T09:00:00Z"
ENTRY_TEXT = f"[entry text] Order A-104 is ready, code {CODE}."
CONTINUATION_TEXT = f"[continuation text] Order A-104 is ready, code {CODE}."
JUDGE_TEXT = f"[judge] Order A-104 is ready, code {CODE}."
SECOND_TURN_TEXT = f"[second turn] Order A-104 is ready, code {CODE}."
LOOP_MARK = "[loop]"
FINALIZATION = ('<finalization_status>{"status":"final_answer","reasoning":"The order status '
                'and pickup code were retrieved with lookup_order."}</finalization_status>')
MISSING_LIVE = "Missing live-data evidence for a freshness-sensitive answer."
MISSING_DATE = "Missing exact-date evidence for an exact-date freshness answer."
NEW_FIELDS = ("turn_evidence", "turn_evidence_trace_id")

LIVE_EXACT = {"time_sensitive": True, "needs_live_data": True, "needs_sources": True,
              "needs_exact_date": True, "answer_mode": "browse_then_answer"}
LIVE_NO_DATE = {**LIVE_EXACT, "needs_exact_date": False}
SOURCES_ONLY = {"time_sensitive": True, "needs_live_data": False, "needs_sources": True,
                "needs_exact_date": False, "answer_mode": "local_only"}
NOT_TIME_SENSITIVE = {"time_sensitive": False, "needs_live_data": False, "needs_sources": False,
                      "needs_exact_date": False, "answer_mode": "local_only"}

# ---------------------------------------------------------------- loopback only
BLOCKED: list[str] = []
_real_connect = socket.socket.connect


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    host = address[0] if isinstance(address, tuple) and address else address
    if self.family in (socket.AF_INET, socket.AF_INET6) and str(host) not in {"127.0.0.1", "::1", "localhost"}:
        BLOCKED.append(str(address))
        raise ConnectionRefusedError(f"probe blocks non-loopback connect to {address!r}")
    return _real_connect(self, address)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign]


# ---------------------------------------------------------------- scripted provider
def completion(message: dict[str, Any], finish: str) -> dict[str, Any]:
    return {"id": "chatcmpl-fixture", "object": "chat.completion", "created": 0, "model": "fixture-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}


def tool_call(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    call = {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
    return completion({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")


def text(content: str) -> dict[str, Any]:
    return completion({"role": "assistant", "content": content}, "stop")


def all_text(body: dict[str, Any]) -> str:
    parts = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        parts.append(str(content or ""))
    return "\n".join(parts)


class TurnPlan:
    """What the scripted model answers during one user turn.

    classifier: the freshness classifier's answer (sets the typed obligations).
    first_entry: "text" (the first entry step answers in text) or "tool"
                 (it calls lookup_order).
    judges: one verdict per judge call ("close" or "continue"); the last repeats.
    continuations: one answer per continuation entry step ("text" or "tool");
                   the last repeats.
    entry_text: the text an entry step answers with.
    """

    def __init__(self, *, classifier: dict[str, Any], first_entry: str, judges: tuple[str, ...] = ("close",),
                 continuations: tuple[str, ...] = ("text",), entry_text: str = ENTRY_TEXT,
                 continuation_text: str = CONTINUATION_TEXT) -> None:
        self.classifier = classifier
        self.first_entry = first_entry
        self.judges = judges
        self.continuations = continuations
        self.entry_text = entry_text
        self.continuation_text = continuation_text


class Script:
    def __init__(self, turns: list[TurnPlan]) -> None:
        self.turns = turns
        self.turn = 0
        self.kinds: list[str] = []
        self.models: list[str] = []
        self.per_turn: dict[int, dict[str, int]] = {}
        # Tool-call ids must stay unique across processes that continue one conversation.
        self.prefix = uuid.uuid4().hex[:8]
        self.lock = threading.Lock()

    def respond(self, body: dict[str, Any]) -> dict[str, Any]:
        txt = all_text(body)
        choice = body.get("tool_choice")
        forced = str((choice.get("function") or {}).get("name", "")) if isinstance(choice, dict) else ""
        after_tool = any(m.get("role") == "tool" for m in body.get("messages") or [])
        if forced == "submit_output":
            if "purpose: judge" in txt or "[JUDGE CONTEXT]" in txt:
                kind = "judge"
            elif "purpose: reflect" in txt:
                kind = "reflect"
            elif "freshness-routing classifier" in txt:
                kind = "freshness"
            else:
                kind = "other_structured"
        elif "purpose: decide" in txt:
            kind = "entry_after_tool" if after_tool else "entry"
        else:
            kind = "act_loop" if after_tool else "other_text"
        with self.lock:
            index = len(self.kinds)
            plan = self.turns[min(self.turn, len(self.turns) - 1)]
            counts = self.per_turn.setdefault(self.turn, {})
            # A later turn carries the earlier turns' tool messages, so "after tool"
            # is counted per turn: the first entry step of each turn is "entry".
            if kind in ("entry", "entry_after_tool"):
                kind = "entry" if counts.get("entry_steps", 0) == 0 else "entry_after_tool"
                counts["entry_steps"] = counts.get("entry_steps", 0) + 1
            n = counts.get(kind, 0)
            counts[kind] = n + 1
            self.kinds.append(f"t{self.turn + 1}:{kind}")
            self.models.append(str(body.get("model")))
        if kind == "reflect":
            item = {"title": "Check order A-104", "normalized_key": "task:check_order_a104", "kind": "task",
                    "content": "Check order A-104"}
            return tool_call("submit_output", {"items": [item]}, f"call-r-{self.prefix}-{index}")
        if kind == "freshness":
            answer = {"intent": "order_status_inquiry", "domain": "shopping", **plan.classifier,
                      "reason": "Order status questions.", "confidence": 1}
            return tool_call("submit_output", answer, f"call-f-{self.prefix}-{index}")
        if kind == "judge":
            verdict_kind = plan.judges[min(n, len(plan.judges) - 1)]
            if verdict_kind == "continue":
                verdict = {"final_answer": JUDGE_TEXT, "satisfied": False, "next_action": "continue",
                           "reason": "Summarize the observed order status in a final answer."}
            else:
                verdict = {"final_answer": JUDGE_TEXT, "satisfied": True, "next_action": "close"}
            return tool_call("submit_output", verdict, f"call-j-{self.prefix}-{index}")
        if kind == "entry":
            if plan.first_entry == "text":
                return text(plan.entry_text)
            return tool_call("lookup_order", {"order_id": "A-104"}, f"call-l-{self.prefix}-{index}")
        if kind == "entry_after_tool":
            step = plan.continuations[min(n, len(plan.continuations) - 1)]
            if step == "tool":
                return tool_call("lookup_order", {"order_id": "A-104"}, f"call-l-{self.prefix}-{index}")
            return text(plan.continuation_text)
        if kind == "act_loop":
            return text(f"{LOOP_MARK} Order A-104 is ready, code {CODE}.\n\n{FINALIZATION}")
        if kind == "other_structured":
            return tool_call("submit_output", {}, f"call-o-{self.prefix}-{index}")
        return text("ok")


# ---------------------------------------------------------------- observers (read-only wrappers)
def _obligations(obligations: Any) -> list[bool] | None:
    if obligations is None:
        return None
    return [obligations.require_live_data, obligations.require_sources, obligations.require_exact_date]


def _result_view(action_result: Any) -> dict[str, Any] | None:
    if action_result is None:
        return None
    outputs = getattr(action_result, "outputs", None) or {}
    tool_results = outputs.get("tool_results") if isinstance(outputs, dict) else None
    names = [str(t.get("tool_name")) for t in tool_results or [] if isinstance(t, dict)]
    return {"command_id": getattr(action_result, "command_id", None),
            "status": getattr(action_result, "status", None),
            "summary": str(getattr(action_result, "summary", "") or "")[:120],
            "has_outputs": bool(outputs), "tool_result_names": names,
            "dated": DATED_AT in json.dumps(outputs, default=str)}


class Observers:
    """Wraps (does not replace) the installed functions and records calls."""

    def __init__(self) -> None:
        self.records: dict[str, list[dict[str, Any]]] = {"entry": [], "respond_verify": [], "guard": [],
                                                         "gate": [], "brain_entry": []}
        self._saved: list[tuple[Any, str, Any]] = []

    def __enter__(self) -> "Observers":
        from openminion.modules.brain.diagnostics.events import CanonicalEventLogger
        from openminion.modules.brain.execution.closure import base as closure_base
        from openminion.modules.brain.execution.runtime.closure import evaluator
        from openminion.modules.brain.loop import orchestration

        recs = self.records
        real_entry = orchestration._apply_entry_freshness

        def entry(**kwargs: Any) -> Any:
            result = real_entry(**kwargs)
            st = kwargs["state"]
            response = kwargs.get("response")
            recs["entry"].append({"user_input": kwargs.get("user_input"),
                                  "response": "tool call" if getattr(response, "tool_calls", None) else "text",
                                  "obligations": _obligations(st.freshness_obligations),
                                  "trace_id": getattr(st, "trace_id", None)})
            return result

        real_close_verify = closure_base.verify_freshness_answer

        def close_verify(**kwargs: Any) -> list[str]:
            reasons = real_close_verify(**kwargs)
            caller = sys._getframe(1)
            outer = caller.f_back
            if caller.f_code.co_name == "final_close_message":
                rec: dict[str, Any] = {
                    "caller": caller.f_code.co_name,
                    "called_from": outer.f_code.co_name if outer is not None else None,
                    "answer": kwargs.get("answer"),
                    "time_sensitive": getattr(kwargs.get("contract"), "time_sensitive", None),
                    "obligations": _obligations(kwargs.get("obligations")),
                    "evidence": _result_view(kwargs.get("action_result")),
                    "result": list(reasons)}
                ctx = outer.f_locals.get("ctx") if outer is not None else None
                st = getattr(ctx, "state", None)
                if st is not None:
                    te = getattr(st, "turn_evidence", None)
                    rec["state_at_respond"] = {
                        "trace_id": getattr(st, "trace_id", None),
                        "turn_evidence_trace_id": getattr(st, "turn_evidence_trace_id", "<absent>"),
                        "turn_evidence_command_id": getattr(te, "command_id", None),
                        "trace_ids_match": (getattr(st, "turn_evidence_trace_id", None) == getattr(st, "trace_id", None)),
                    }
                recs["respond_verify"].append(rec)
            return reasons

        real_guard_verify = evaluator.verify_freshness_answer

        def guard_verify(**kwargs: Any) -> list[str]:
            reasons = real_guard_verify(**kwargs)
            if sys._getframe(1).f_code.co_name == "_apply_freshness_guard":
                recs["guard"].append({"obligations": _obligations(kwargs.get("obligations")),
                                      "evidence": _result_view(kwargs.get("action_result")),
                                      "result": list(reasons)})
            return reasons

        real_emit = CanonicalEventLogger.emit

        def emit(logger_self: Any, event_type: str, payload: dict[str, Any], *a: Any, **k: Any) -> Any:
            if event_type == "brain.closure_gate.completed":
                recs["gate"].append({"next_action": payload.get("next_action"), "reason": payload.get("reason")})
            elif event_type == "brain.entry":
                recs["brain_entry"].append({"route": payload.get("route"), "reason_code": payload.get("reason_code")})
            return real_emit(logger_self, event_type, payload, *a, **k)

        for owner, name, value in ((orchestration, "_apply_entry_freshness", entry),
                                   (closure_base, "verify_freshness_answer", close_verify),
                                   (evaluator, "verify_freshness_answer", guard_verify),
                                   (CanonicalEventLogger, "emit", emit)):
            self._saved.append((owner, name, getattr(owner, name)))
            setattr(owner, name, value)
        return self

    def __exit__(self, *exc: Any) -> None:
        for owner, name, value in reversed(self._saved):
            setattr(owner, name, value)


# ---------------------------------------------------------------- SDK scenario
def write_sdk_config(root: Path, port: int) -> Path:
    from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config

    config = OpenMinionConfig()
    config.agents = {"openminion": AgentProfileConfig(name="openminion", provider="openai", default_channel="console")}
    config.default_agent = "openminion"
    config.providers.openai.api_key_env = "TEST_PROVIDER_KEY"
    config.providers.openai.base_url = f"http://127.0.0.1:{port}/v1"
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / "state.db")
    root.mkdir(parents=True, exist_ok=True)
    save_config(config, str(root / "agents.json"))
    return root / "agents.json"


class LoopbackProvider:
    def __init__(self, script: Script) -> None:
        self.script = script
        script_ref = script

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                payload = json.dumps(script_ref.respond(body)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "LoopbackProvider":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def run_scenario(root: Path, script: Script, *, inputs: list[str], forced_tools: list[str],
                 dated_lookups: tuple[bool, ...] = (False,), session_id: str = "s419",
                 conversation_id: str | None = None, observe: bool = True) -> dict[str, Any]:
    """Run `inputs` as consecutive turns of one SDK Agent on a fresh or existing root.

    conversation_id: when set, each turn's payload carries it, as the HTTP turns
    route and the interactive CLI send one, so a later turn starts from the
    earlier turn's working state (the same input upstream's two-turn test uses).
    """
    from openminion import Agent, APIRuntime, tool

    root.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {"inputs": list(inputs), "forced_tools": list(forced_tools),
                           "conversation_id": conversation_id}
    calls: list[dict[str, Any]] = []
    old_cwd = os.getcwd()
    old_env = {k: os.environ.get(k) for k in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT", "TEST_PROVIDER_KEY")}
    real_build_payload = Agent._build_payload
    with LoopbackProvider(script) as provider:
        os.chdir(root)
        os.environ["OPENMINION_HOME"] = str(root)
        os.environ["OPENMINION_DATA_ROOT"] = str(root / "data")
        os.environ["TEST_PROVIDER_KEY"] = "local-dummy-key"
        try:
            cfg = write_sdk_config(root, provider.port)

            @tool(min_scope="READ_ONLY")
            def lookup_order(order_id: str) -> Any:
                """Return the current status and pickup code for one order."""
                dated = dated_lookups[min(len(calls), len(dated_lookups) - 1)]
                calls.append({"order_id": order_id, "dated": dated})
                if dated:
                    return {"order_id": order_id, "status": "ready for pickup", "pickup_code": CODE,
                            "observed_at": DATED_AT}
                return f"order {order_id}: ready for pickup, pickup code {CODE}"

            if conversation_id:
                def build_payload(self: Any, message: str, **kwargs: Any) -> dict[str, Any]:
                    payload = real_build_payload(self, message, **kwargs)
                    payload["conversation_id"] = conversation_id
                    return payload

                Agent._build_payload = build_payload  # type: ignore[method-assign]
            runtime = APIRuntime.from_config_path(str(cfg), home_root=root, data_root=root / "data")
            obs = Observers() if observe else None
            try:
                if obs is not None:
                    obs.__enter__()
                with Agent(runtime=runtime, instructions="Use lookup_order, then answer in one sentence with the code.",
                           tools=[lookup_order], forced_tools=forced_tools) as agent:
                    results = []
                    for i, message in enumerate(inputs):
                        script.turn = i
                        try:
                            res = agent.run(message, session_id=session_id, timeout_seconds=120)
                            results.append({"run_state": res.run_state, "text": res.text,
                                            "session_id": res.session_id})
                        except Exception as exc:  # noqa: BLE001 - recorded
                            results.append({"error": f"{type(exc).__name__}: {str(exc)[:600]}"})
                    out["results"] = results
            finally:
                if obs is not None:
                    obs.__exit__(None, None, None)
                runtime.close()
        finally:
            Agent._build_payload = real_build_payload  # type: ignore[method-assign]
            os.chdir(old_cwd)
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    if obs is not None:
        out.update(obs.records)
    out["provider_request_kinds"] = list(script.kinds)
    out["provider_request_models"] = sorted(set(script.models))
    out["lookup_order_calls"] = calls
    return out


# ---------------------------------------------------------------- stored working state
def brain_sessions_db(root: Path) -> Path:
    """The brain's SQLite session store that APIRuntime uses: <storage dir>/brain/sessions.db."""
    from openminion.modules.brain.paths import resolve_brain_sessions_db_path

    return resolve_brain_sessions_db_path(storage_path=root / "state.db")


def tables_with_working_state(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.db")):
        try:
            con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            try:
                names = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
            finally:
                con.close()
        except sqlite3.Error:
            continue
        if "working_state" in names:
            found.append(str(path.relative_to(root)).replace("\\", "/"))
    return found


def _evidence_view(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return raw
    return {"command_id": raw.get("command_id"), "status": raw.get("status"),
            "summary": str(raw.get("summary") or "")[:120], "has_outputs": bool(raw.get("outputs")),
            "dated": DATED_AT in json.dumps(raw.get("outputs"), default=str)}


def state_view(payload: dict[str, Any]) -> dict[str, Any]:
    """The #419 fields of one stored working-state payload (a dict)."""
    view: dict[str, Any] = {"session_id": payload.get("session_id"), "trace_id": payload.get("trace_id"),
                            "has_turn_evidence_key": "turn_evidence" in payload,
                            "has_turn_evidence_trace_id_key": "turn_evidence_trace_id" in payload}
    view["turn_evidence_trace_id"] = payload.get("turn_evidence_trace_id", "<absent>")
    view["turn_evidence"] = _evidence_view(payload.get("turn_evidence", "<absent>"))
    last = payload.get("last_result")
    view["last_result_command_id"] = last.get("command_id") if isinstance(last, dict) else None
    diag = payload.get("freshness_diagnostics")
    view["verifier_notes"] = list(diag.get("verifier_notes") or []) if isinstance(diag, dict) else None
    return view


def raw_sqlite_states(db: Path) -> list[dict[str, Any]]:
    """Every working_state row, read with a read-only sqlite3 connection."""
    con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cols = [r[1] for r in con.execute("pragma table_info(working_state)")]
        order = "rowid"
        rows = [dict(r) for r in con.execute(f"select * from working_state order by {order}")]
    finally:
        con.close()
    out = []
    for row in rows:
        inline = None
        for key in ("state_inline_json", "state_inline", "state_json", "payload_json"):
            if key in row and row[key]:
                inline = json.loads(row[key]) if isinstance(row[key], str) else row[key]
                break
        rec = {k: row[k] for k in cols if k not in ("state_inline_json", "state_inline", "state_json", "payload_json")}
        rec["state"] = state_view(inline) if isinstance(inline, dict) else None
        out.append(rec)
    return out


def local_state_files(root: Path) -> dict[str, dict[str, Any]]:
    """working_state.json files of a local session store, read as plain JSON."""
    out = {}
    for path in sorted(Path(root).rglob("working_state.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        inline = data.get("state_inline") if isinstance(data, dict) else None
        out[str(path.relative_to(root)).replace("\\", "/")] = {
            "version": data.get("version") if isinstance(data, dict) else None,
            "state": state_view(inline) if isinstance(inline, dict) else None}
    return out


def reader_profile(agent_id: str = "openminion") -> Any:
    from openminion.modules.brain.schemas import AgentBudgets, AgentDefaults, AgentProfile, LLMProfiles

    return AgentProfile(agent_id=agent_id, role="general",
                        llm_profiles=LLMProfiles(decide_model="decide-default", plan_model="plan-default",
                                                 act_model=None, reflect_model="reflect-default",
                                                 summarize_model="summarize-default"),
                        budgets=AgentBudgets(max_ticks_per_user_turn=20, max_tool_calls=5, max_a2a_calls=5,
                                             max_total_llm_tokens=5000, max_elapsed_ms=120000),
                        defaults=AgentDefaults())


def owner_runner(session_api: Any, agent_id: str = "openminion") -> Any:
    from openminion.modules.brain.config import RunnerOptions
    from openminion.modules.brain.runner import BrainRunner

    return BrainRunner(profile=reader_profile(agent_id), session_api=session_api,
                       options=RunnerOptions(metactl_enabled=False))


def same_turn_evidence_fn() -> Any:
    from openminion.modules.brain.execution import dispatch

    return getattr(dispatch, "_same_turn_evidence", None)


def loaded_view(state: Any) -> dict[str, Any]:
    fn = same_turn_evidence_fn()
    te = getattr(state, "turn_evidence", "<absent>")
    view = {"trace_id": state.trace_id,
            "turn_evidence_trace_id": getattr(state, "turn_evidence_trace_id", "<absent>"),
            "turn_evidence": None if te is None else ("<absent>" if te == "<absent>" else
                                                      {"command_id": te.command_id, "status": te.status,
                                                       "dated": DATED_AT in json.dumps(te.outputs, default=str)}),
            "last_result_command_id": getattr(state.last_result, "command_id", None),
            "goal": state.goal}
    if fn is None:
        view["same_turn_evidence"] = "<_same_turn_evidence absent in this version>"
    else:
        ev = fn(state)
        view["same_turn_evidence"] = None if ev is None else ev.command_id
    return view


def load_through_owner(kind: str, location: Path, session_id: str, agent_id: str = "openminion") -> dict[str, Any]:
    """Load one session's working state with the installed brain owner.

    kind "sqlite": SessctlAdapter(<sessions.db>); kind "local": LocalSessionStore(<sessions dir>).
    Returns the loaded view, or the error type and pydantic error types/locations.
    """
    from openminion.modules.brain.adapters.session import LocalSessionStore, SessctlAdapter
    from openminion.modules.brain.state import load_or_init_state

    api = SessctlAdapter(location) if kind == "sqlite" else LocalSessionStore(location)
    writes: list[str] = []
    real_put = api.put_working_state

    def counting_put(sid: str, **kwargs: Any) -> Any:
        writes.append(sid)
        return real_put(sid, **kwargs)

    api.put_working_state = counting_put  # type: ignore[method-assign]
    try:
        runner = owner_runner(api, agent_id)
        try:
            state = load_or_init_state(runner, session_id)
        except Exception as exc:  # noqa: BLE001 - recorded
            errors = None
            if hasattr(exc, "errors"):
                try:
                    errors = [{"type": e.get("type"), "loc": list(e.get("loc", ()))} for e in exc.errors()]
                except Exception:  # noqa: BLE001
                    errors = None
            return {"ok": False, "error": type(exc).__name__, "errors": errors, "writes_during_load": len(writes)}
        return {"ok": True, "state": loaded_view(state), "writes_during_load": len(writes), "_state": state}
    finally:
        close = getattr(api, "close", None)
        if callable(close):
            close()


def public(view: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in view.items() if not k.startswith("_")}
