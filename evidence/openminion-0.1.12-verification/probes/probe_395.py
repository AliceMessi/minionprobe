"""#395 on the installed package: the turn's freshness contract on an entry step
with no new user input and no typed contract.

The model is a scripted OpenAI-compatible provider served by ThreadingHTTPServer
on 127.0.0.1 (the `openai` provider with base_url http://127.0.0.1:<port>/v1, a
dummy key, model name as configured; no external network). It is the scenario
of upstream tests/api/test_freshness_continuation.py, rebuilt without repo
fixtures: `Agent(tools=[lookup_order], forced_tools=["lookup_order"])` runs
"Check order A-104"; the freshness classifier asks for an exact date,
`lookup_order` returns no dated field and the judge closes, so the closure
freshness guard turns the close into a continue and the turn takes a
continuation entry step with no user input. `lookup_order`'s schema has no
`freshness` field, so that entry carries no typed contract unless the script
adds one.

Read-only observers (wrapping, not replacing): each `_apply_entry_freshness`
call (user_input; the typed contract, obligations and diagnostics before and
after, as full dumps and object identity), each closure freshness guard check,
every closure gate, `brain.freshness.entry_fallback` and
`brain.freshness.classified` events.

Scenarios:
  B   continuation entry answers with text (no input, no contract)
  E   continuation entry calls lookup_order again without a contract, then text
  A1  control: continuation entry calls lookup_order with a valid typed contract
  N1  control: a second turn with new user input
  F   control: a first entry step with empty input and no prior contract
      (SDK `agent.run("")`; plus the installed `_apply_entry_freshness` on a
      fresh WorkingState, as upstream's unit test does)

Usage: python -I -X utf8 probe_395.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()
info = banner("probe_395", provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              fixture_response_model="fixture-model")

from openminion import Agent, APIRuntime, tool  # noqa: E402
from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config  # noqa: E402
from openminion.modules.brain.diagnostics.events import CanonicalEventLogger  # noqa: E402
from openminion.modules.brain.execution.runtime.closure import evaluator  # noqa: E402
from openminion.modules.brain.loop import orchestration  # noqa: E402

c = Checks()
observed: dict[str, Any] = {}
CODE = "ZEBRA-42"
LIVE_NO_DATE = {"domain": "shopping", "time_sensitive": True, "needs_live_data": True, "needs_sources": True,
                "needs_exact_date": False, "answer_mode": "browse_then_answer"}
FINALIZATION = ('<finalization_status>{"status":"final_answer","reasoning":"The order status '
                'and pickup code were retrieved with lookup_order."}</finalization_status>')
MISSING_DATE = "Missing exact-date evidence for an exact-date freshness answer."
EXACT = {"needs_exact_date": True, "time_sensitive": True, "require_exact_date": True, "classifier_mode": "llm"}


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


class Script:
    """`continuation`: text | tool_then_text | contract_then_text (what continuation entries answer)."""

    def __init__(self, continuation: str) -> None:
        self.continuation = continuation
        self.continuation_entries = 0
        self.kinds: list[str] = []
        self.models: list[str] = []
        self.lock = threading.Lock()

    def respond(self, body: dict[str, Any]) -> dict[str, Any]:
        txt = all_text(body)
        choice = body.get("tool_choice")
        forced = str((choice.get("function") or {}).get("name", "")) if isinstance(choice, dict) else ""
        messages = body.get("messages") or []
        after_tool = any(m.get("role") == "tool" for m in messages)
        users = [str(m.get("content") or "") for m in messages if m.get("role") == "user"]
        request = next((u for u in users if u.startswith("Check order")), "")
        order = "B-205" if "B-205" in request else "A-104"
        with self.lock:
            index = len(self.kinds)
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
            self.kinds.append(kind)
            self.models.append(str(body.get("model")))
            if kind == "entry_after_tool":
                self.continuation_entries += 1
            cont = self.continuation_entries
        if kind == "reflect":
            return tool_call("submit_output", {"items": [{"title": f"Check order {order}",
                                                          "normalized_key": f"task:check_order_{order}",
                                                          "kind": "task", "content": f"Check order {order}"}]},
                             f"call-reflect-{index}")
        if kind == "freshness":
            return tool_call("submit_output", {"intent": "order_status_inquiry", "domain": "shopping",
                                               "time_sensitive": True, "needs_live_data": True, "needs_sources": True,
                                               "needs_exact_date": "B-205" not in txt,
                                               "answer_mode": "browse_then_answer",
                                               "reason": "Order status is live data.", "confidence": 1},
                             f"call-freshness-{index}")
        if kind == "judge":
            return tool_call("submit_output", {"final_answer": f"[judge] Order {order} is ready, code {CODE}.",
                                               "satisfied": True, "next_action": "close"}, f"call-judge-{index}")
        if kind == "entry":
            return tool_call("lookup_order", {"order_id": order}, f"call-lookup-{index}")
        if kind == "entry_after_tool":
            if cont == 1 and self.continuation == "tool_then_text":
                return tool_call("lookup_order", {"order_id": order}, f"call-lookup-{index}")
            if cont == 1 and self.continuation == "contract_then_text":
                return tool_call("lookup_order", {"order_id": order, "freshness": dict(LIVE_NO_DATE)},
                                 f"call-lookup-{index}")
            return text(f"[continuation] Order {order} is ready, code {CODE}.")
        if kind == "act_loop":
            return text(f"[loop] Order {order} is ready, code {CODE}.\n\n{FINALIZATION}")
        if kind == "other_structured":
            return tool_call("submit_output", {}, f"call-other-{index}")
        return text("ok")


# ---------------------------------------------------------------- observers
class Observers:
    def __init__(self) -> None:
        self.records: dict[str, list[dict[str, Any]]] = {"entry": [], "guard": [], "gate": [], "fallback": [],
                                                         "classified": []}
        self._saved: list[tuple[Any, str, Any]] = []

    @staticmethod
    def typed(state: Any) -> dict[str, Any] | None:
        if state.freshness_contract is None:
            return None
        return {"needs_exact_date": state.freshness_contract.needs_exact_date,
                "time_sensitive": state.freshness_contract.time_sensitive,
                "require_exact_date": state.freshness_obligations.require_exact_date,
                "classifier_mode": state.freshness_diagnostics.classifier_mode}

    @staticmethod
    def full(state: Any) -> dict[str, Any] | None:
        if state.freshness_contract is None:
            return None
        return {"contract": state.freshness_contract.model_dump(mode="json"),
                "obligations": state.freshness_obligations.model_dump(mode="json"),
                "diagnostics": state.freshness_diagnostics.model_dump(mode="json")}

    def __enter__(self) -> "Observers":
        recs = self.records
        real_entry = orchestration._apply_entry_freshness

        def entry(**kwargs: Any) -> Any:
            st = kwargs["state"]
            ids_before = (id(st.freshness_contract), id(st.freshness_obligations), id(st.freshness_diagnostics))
            before, full_before = self.typed(st), self.full(st)
            result = real_entry(**kwargs)
            ids_after = (id(st.freshness_contract), id(st.freshness_obligations), id(st.freshness_diagnostics))
            recs["entry"].append({"user_input": kwargs.get("user_input"), "before": before, "after": self.typed(st),
                                  "full_before": full_before, "full_after": self.full(st),
                                  "same_objects": ids_before == ids_after, "llm_call_id": kwargs.get("llm_call_id")})
            return result

        real_verify = evaluator.verify_freshness_answer

        def verify(**kwargs: Any) -> list[str]:
            reasons = real_verify(**kwargs)
            if sys._getframe(1).f_code.co_name == "_apply_freshness_guard":
                recs["guard"].append({"require_exact_date": kwargs["obligations"].require_exact_date,
                                      "result": list(reasons)})
            return reasons

        real_emit = CanonicalEventLogger.emit

        def emit(logger_self: Any, event_type: str, payload: dict[str, Any], *a: Any, **k: Any) -> Any:
            if event_type == "brain.closure_gate.completed":
                recs["gate"].append({"next_action": payload.get("next_action"), "reason": payload.get("reason")})
            elif event_type == "brain.freshness.entry_fallback":
                recs["fallback"].append({"reason": payload.get("reason"), "llm_call_id": payload.get("llm_call_id")})
            elif event_type == "brain.freshness.classified":
                recs["classified"].append({k2: payload.get(k2) for k2 in ("classifier_mode", "llm_call_id")})
            return real_emit(logger_self, event_type, payload, *a, **k)

        for owner, name, value in ((orchestration, "_apply_entry_freshness", entry),
                                   (evaluator, "verify_freshness_answer", verify),
                                   (CanonicalEventLogger, "emit", emit)):
            self._saved.append((owner, name, getattr(owner, name)))
            setattr(owner, name, value)
        return self

    def __exit__(self, *exc: Any) -> None:
        for owner, name, value in reversed(self._saved):
            setattr(owner, name, value)


def run_scenario(name: str, continuation: str, inputs: list[str]) -> dict[str, Any]:
    script = Script(continuation)
    root = WORK / name
    root.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
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
    old_cwd = os.getcwd()
    old_env = {k: os.environ.get(k) for k in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT", "TEST_PROVIDER_KEY")}
    os.chdir(root)
    os.environ["OPENMINION_HOME"] = str(root)
    os.environ["OPENMINION_DATA_ROOT"] = str(root / "data")
    os.environ["TEST_PROVIDER_KEY"] = "local-dummy-key"
    calls: list[str] = []
    out: dict[str, Any] = {"inputs": inputs, "continuation_script": continuation}
    try:
        config = OpenMinionConfig()
        config.agents = {"openminion": AgentProfileConfig(name="openminion", provider="openai",
                                                          default_channel="console")}
        config.default_agent = "openminion"
        config.providers.openai.api_key_env = "TEST_PROVIDER_KEY"
        config.providers.openai.base_url = f"http://127.0.0.1:{server.server_port}/v1"
        config.runtime.process_mode = "single-process"
        config.runtime.daemon_auto_start = False
        config.storage.path = str(root / "state.db")
        save_config(config, str(root / "agents.json"))
        out["configured_model"] = str(getattr(config.providers.openai, "model", None) or
                                      getattr(config.providers.openai, "default_model", None))

        @tool(min_scope="READ_ONLY")
        def lookup_order(order_id: str) -> str:
            """Return the current status and pickup code for one order."""
            calls.append(order_id)
            return f"order {order_id}: ready for pickup, pickup code {CODE}"

        runtime = APIRuntime.from_config_path(str(root / "agents.json"), home_root=root, data_root=root / "data")
        with Observers() as obs:
            try:
                with Agent(runtime=runtime, instructions="Use lookup_order, then answer in one sentence with the code.",
                           tools=[lookup_order], forced_tools=["lookup_order"]) as agent:
                    results = []
                    for text_in in inputs:
                        try:
                            res = agent.run(text_in, session_id=f"fresh-{name}", timeout_seconds=120)
                            results.append({"run_state": res.run_state, "text": res.text})
                        except Exception as exc:  # noqa: BLE001
                            results.append({"error": f"{type(exc).__name__}: {exc}"})
                    out["results"] = results
            finally:
                runtime.close()
        out.update(obs.records)
        out["provider_request_kinds"] = script.kinds
        out["provider_request_models"] = sorted(set(script.models))
        out["lookup_order_calls"] = calls
    finally:
        os.chdir(old_cwd)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        server.shutdown()
        server.server_close()
        thread.join()
    observed[name] = out
    return out


def first_step_blocked(name: str, out: dict[str, Any]) -> None:
    e0 = out["entry"][0] if out["entry"] else None
    c.check(f"{name}: first entry step classifies the user input (exact date required)",
            e0 is not None and (e0["user_input"], e0["before"], e0["after"]) == ("Check order A-104", None, EXACT),
            None if e0 is None else [e0["user_input"], e0["before"], e0["after"]])
    c.check(f"{name}: the closure freshness guard blocks the judge's close (missing exact-date evidence) and the gate "
            f"continues the turn",
            out["guard"][:1] == [{"require_exact_date": True, "result": [MISSING_DATE]}]
            and out["gate"][:1] == [{"next_action": "continue", "reason": "freshness_verifier_blocked"}],
            {"guard": out["guard"][:1], "gate": out["gate"][:1]})


def retained(name: str, out: dict[str, Any]) -> None:
    e1 = out["entry"][1] if len(out["entry"]) > 1 else None
    c.check(f"{name}: continuation entry (no user input, no typed contract) keeps the contract: before == after == "
            f"exact date required, classifier_mode llm",
            e1 is not None and not str(e1["user_input"] or "").strip() and e1["before"] == EXACT and e1["after"] == EXACT,
            None if e1 is None else [e1["user_input"], e1["before"], e1["after"]])
    c.check(f"{name}: the contract, obligations and diagnostics are the same objects with identical full dumps "
            f"(classifier_model, classified_at, notes, all obligations)",
            e1 is not None and e1["same_objects"] and e1["full_before"] == e1["full_after"]
            and e1["full_after"] is not None,
            None if e1 is None else {"same_objects": e1["same_objects"], "equal": e1["full_before"] == e1["full_after"],
                                     "obligations_after": (e1["full_after"] or {}).get("obligations")})
    fb = [f for f in out["fallback"] if e1 is not None and f["llm_call_id"] == e1["llm_call_id"]]
    c.check(f"{name}: that step emits brain.freshness.entry_fallback with reason retained_turn_contract",
            [f["reason"] for f in fb] == ["retained_turn_contract"], fb)
    c.check(f"{name}: the freshness classifier is called once in the turn (no reclassification)",
            out["provider_request_kinds"].count("freshness") == 1, out["provider_request_kinds"].count("freshness"), 1)


# ---------------------------------------------------------------- B, E, A1, N1, F
b = run_scenario("B", "text", ["Check order A-104"])
first_step_blocked("B", b)
retained("B", b)
c.check("B: run completes", [r.get("run_state") for r in b["results"]] == ["completed"], b["results"])

e = run_scenario("E", "tool_then_text", ["Check order A-104"])
first_step_blocked("E", e)
retained("E", e)
c.check("E: the second close, on the same undated lookup_order output, is blocked too",
        e["guard"][1:2] == [{"require_exact_date": True, "result": [MISSING_DATE]}]
        and e["gate"][1:2] == [{"next_action": "continue", "reason": "freshness_verifier_blocked"}],
        {"guard": e["guard"][1:2], "gate": e["gate"][1:2]})
c.check("E: lookup_order ran twice and the judge's unverified answer is not the result",
        e["lookup_order_calls"] == ["A-104", "A-104"] and "[judge]" not in str(e["results"][0].get("text")),
        {"calls": e["lookup_order_calls"], "text": e["results"][0].get("text")})

a1 = run_scenario("A1", "contract_then_text", ["Check order A-104"])
first_step_blocked("A1", a1)
want_a1 = {"needs_exact_date": False, "time_sensitive": True, "require_exact_date": False,
           "classifier_mode": "entry_contract"}
e1 = a1["entry"][1] if len(a1["entry"]) > 1 else None
c.check("A1 (control): a valid typed contract on the continuation entry replaces the turn's contract",
        e1 is not None and e1["before"] == EXACT and e1["after"] == want_a1,
        None if e1 is None else [e1["before"], e1["after"]], want_a1)
c.check("A1 (control): the replaced contract needs no exact date, so the second close passes and the judge answers",
        a1["guard"][1:2] == [{"require_exact_date": False, "result": []}]
        and str(a1["results"][0].get("text", "")).endswith(f"[judge] Order A-104 is ready, code {CODE}."),
        {"guard": a1["guard"][1:2], "text": a1["results"][0].get("text")})
c.check("A1 (control): no retained_turn_contract fallback on the typed-contract step",
        all(f["reason"] != "retained_turn_contract" for f in a1["fallback"]), a1["fallback"])

n1 = run_scenario("N1", "text", ["Check order A-104", "Check order B-205"])
turn_two = [s for s in n1["entry"] if s["user_input"] == "Check order B-205"]
want_n1 = [{"user_input": "Check order B-205", "before": None,
            "after": {"needs_exact_date": False, "time_sensitive": True, "require_exact_date": False,
                      "classifier_mode": "llm"}}]
c.check("N1 (control): new user input on the next turn is classified again (no exact date for B-205)",
        [{k: s[k] for k in ("user_input", "before", "after")} for s in turn_two] == want_n1,
        [{k: s[k] for k in ("user_input", "before", "after")} for s in turn_two], want_n1)
c.check("N1 (control): two classifier calls, both lookups, the second turn answered by the judge",
        n1["provider_request_kinds"].count("freshness") == 2 and n1["lookup_order_calls"] == ["A-104", "B-205"]
        and str(n1["results"][1].get("text", "")).endswith(f"[judge] Order B-205 is ready, code {CODE}."),
        {"freshness_calls": n1["provider_request_kinds"].count("freshness"), "calls": n1["lookup_order_calls"],
         "text": n1["results"][1].get("text") if len(n1["results"]) > 1 else None})

f = run_scenario("F", "text", [""])
first = f["entry"][0] if f["entry"] else None
observed["F_sdk_first_entry"] = first
if first is not None:
    c.check("F (control, SDK agent.run('')): the first entry step with empty input and no prior contract gets the "
            "classifier's empty-input default (skipped_empty_request, no exact date) with reason "
            "missing_or_invalid_contract",
            not str(first["user_input"] or "").strip() and first["before"] is None
            and (first["after"] or {}).get("classifier_mode") == "skipped_empty_request"
            and (first["after"] or {}).get("require_exact_date") is False
            and [x["reason"] for x in f["fallback"] if x["llm_call_id"] == first["llm_call_id"]]
            == ["missing_or_invalid_contract"],
            {"user_input": first["user_input"], "before": first["before"], "after": first["after"],
             "fallback": [x for x in f["fallback"] if x["llm_call_id"] == first["llm_call_id"]]})
else:
    # The SDK refuses an empty message before any brain step, so a first entry step
    # with empty input cannot happen through Agent.run; the installed function is
    # exercised directly below.
    err = str((f.get("results") or [{}])[0].get("error", ""))
    c.check("F (SDK agent.run('')): rejected before any entry step (TurnRequestError: empty message); no entry "
            "step, no classifier call",
            err.startswith("TurnRequestError") and not f["entry"] and f["provider_request_kinds"] == [],
            {"result": f.get("results"), "entry_steps": len(f["entry"]),
             "provider_requests": len(f["provider_request_kinds"])})

# F, installed function on a fresh WorkingState (as upstream's unit test does)
from types import SimpleNamespace  # noqa: E402

from openminion.modules.brain.schemas import BudgetCounters, FreshnessContract, WorkingState  # noqa: E402


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event_type: str, payload: dict[str, Any], **_: Any) -> None:
        self.events.append((event_type, dict(payload)))


state = WorkingState(session_id="s", agent_id="a", trace_id="t", goal="Check order A-104",
                     budgets_remaining=BudgetCounters(ticks=10, tool_calls=5, a2a_calls=2, tokens=2000, time_ms=60000))
rl = RecordingLogger()
runner = SimpleNamespace(llm_api=object(), profile=SimpleNamespace(
    llm_profiles=SimpleNamespace(decide_model="test-model", act_model="")))
orchestration._apply_entry_freshness(runner=runner, state=state, logger=rl,
                                     response=SimpleNamespace(output_text="Order A-104 is ready.", tool_calls=[]),
                                     user_input=None, model="test-model", llm_call_id="entry-call")
fn_obs = {"contract_is_default": state.freshness_contract == FreshnessContract(),
          "require_exact_date": state.freshness_obligations.require_exact_date if state.freshness_obligations else None,
          "classifier_mode": state.freshness_diagnostics.classifier_mode if state.freshness_diagnostics else None,
          "fallback_reasons": [p["reason"] for t, p in rl.events if t == "brain.freshness.entry_fallback"],
          "events": [t for t, _ in rl.events]}
observed["F_function_first_entry"] = fn_obs
c.check("F (control, installed _apply_entry_freshness on a fresh WorkingState, user_input None): default contract, "
        "skipped_empty_request, reason missing_or_invalid_contract",
        fn_obs["contract_is_default"] and fn_obs["require_exact_date"] is False
        and fn_obs["classifier_mode"] == "skipped_empty_request"
        and fn_obs["fallback_reasons"] == ["missing_or_invalid_contract"], fn_obs)

models = sorted({m for k in ("B", "E", "A1", "N1", "F") for m in observed[k]["provider_request_models"]})
observed["provider_request_models"] = models
s = c.summary()
write_result(OUT, {"info": info, "observed": observed, "checks": c.items, "summary": s})
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']} models={models}")
sys.exit(0 if c.all_pass else 1)
