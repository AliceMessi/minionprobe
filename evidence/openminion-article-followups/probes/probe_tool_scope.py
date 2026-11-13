"""Which `@openminion.tool` declarations reach `approval_callback` under the default action policy.

Installed package only (no repo test fixtures); run with `python -I -X utf8`
from an empty directory outside any checkout. It reuses the #448 probe's
helpers (`probe_420_lib.py`, `_common.py`, unchanged): a real `APIRuntime` for
one agent, `openminion`, whose `openai` provider is a scripted
OpenAI-compatible server on 127.0.0.1. Each case gets a fresh runtime home with
the default `action_policy` (mode `auto`, default_action `require_confirm`).
The scripted model calls the case's tool once with {"order_id": "A-104"}.

Public SDK path only: `Agent(instructions=..., tools=[fn], session_id=...,
runtime=runtime).run(message[, approval_callback=cb])`; `runtime=` is the
documented way to pass an `APIRuntime` (the README embed snippet uses the
default config instead, which would need a real provider).

Cases (the tool body is identical; only the decorator differs):
  ARTICLE      @tool(idempotent=True, tags=("read_only",), capabilities=("read_only", "network"))
               as in golive-gate gate.py, approval_callback approves
  BARE         @tool, approval_callback approves
  READ_ONLY    @tool(min_scope="READ_ONLY", idempotent=True), approval_callback approves (control)
  README       the README "Embed an isolated agent session" snippet: bare @tool
               `lookup_order`, instructions "Answer briefly.", session
               "customer-42", "Check order A-104", no approval_callback
  README_FIXED the same with @tool(min_scope="READ_ONLY", idempotent=True), as
               examples/sdk/tool_agent.py declares it since #514 (control)

Plus a provider-free part: the action policy built from a default
`OpenMinionConfig()` (and one with mode "ask" + default_action "allow") is asked
about each declaration, as tests/examples/test_examples.py does.

Exit code: 1 when an EXPECT check fails (the README snippet stops at a policy
confirmation instead of running its tool). CLAIM lines record whether the
article's observation and the code analysis hold; they do not change the exit
code.

Usage: python -I -X utf8 probe.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
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


info = banner("probe_min_scope_default",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "pydantic")},
              openminion_direct_url=_direct_url())

from openminion import Agent, tool  # noqa: E402

import probe_420_lib as lib  # noqa: E402
from probe_420_lib import ScriptedProvider, build_runtime, chdir_env, restore_env  # noqa: E402

ARGS = {"order_id": "A-104"}
PROMPT_MARK = "Policy confirmation required"
DOC = "Return the current status for one order."
DECLS: dict[str, dict[str, Any]] = {
    "ARTICLE": {"idempotent": True, "tags": ("read_only",), "capabilities": ("read_only", "network")},
    "ARTICLE_NO_TAGS": {"idempotent": True},
    "BARE": {},
    "READ_ONLY": {"min_scope": "READ_ONLY", "idempotent": True},
}
expect = Checks()
claim = Checks()


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def make_tool(name: str, decl: dict[str, Any], calls: list[dict[str, Any]], *, docstring: bool = True) -> Any:
    def fn(order_id: str) -> str:
        calls.append({"tool": name, "order_id": order_id})
        return f"order {order_id}: ready"

    fn.__name__ = fn.__qualname__ = name
    fn.__doc__ = DOC if docstring else None
    if not decl:
        return tool(fn)  # same as a bare `@tool`
    return tool(name=name, **decl)(fn)


class ToolCallProvider(ScriptedProvider):
    """The #448 probe's scripted server, plus one call to the armed tool per step."""

    def __init__(self) -> None:
        self.armed: str | None = None
        self.seen_tool_lists: list[list[str]] = []
        super().__init__()

    def respond(self, body: dict[str, Any], path: str) -> dict[str, Any]:
        names = [str((t.get("function") or {}).get("name", "")) for t in body.get("tools") or []
                 if isinstance(t, dict)]
        choice = body.get("tool_choice")
        forced = str((choice.get("function") or {}).get("name", "")) if isinstance(choice, dict) else ""
        match = None
        with self.lock:
            if names:
                self.seen_tool_lists.append(names)
            if forced != "submit_output" and self.armed:
                match = next((n for n in names if _norm(n) == _norm(self.armed)), None)
            if match:
                self.armed = None
                index = len(self.requests)
                self.requests.append({"index": index, "kind": "tool_call", "path": path,
                                      "model": body.get("model"), "t": time.monotonic(), "tool": match})
        if match:
            call = {"id": f"call-{_norm(match)}-{index}", "type": "function",
                    "function": {"name": match, "arguments": json.dumps(ARGS)}}
            return lib._completion({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")
        return super().respond(body, path)


class PolicyRecorder:
    """Call-through recorder on the runtime's action policy controller (`PolicyCtl.check`)."""

    def __init__(self, policy: Any, tool_name: str) -> None:
        self.policy = policy
        self.rows: list[dict[str, Any]] = []
        original = policy.check
        target = _norm(tool_name)

        def check(invocation: Any, ctx: Any, *args: Any, **kwargs: Any) -> Any:
            decision = original(invocation, ctx, *args, **kwargs)
            inv = invocation if isinstance(invocation, dict) else {}
            key = f"{inv.get('tool', '')}.{inv.get('method', '')}"
            if target in _norm(key):
                registered = getattr(policy, "_risk_registry", {}).get(key)
                risk = getattr(decision, "risk", None)
                self.rows.append({
                    "key": key, "decision": getattr(decision, "decision", None),
                    "reason_code": getattr(decision, "reason_code", None),
                    "risk": risk.to_dict() if hasattr(risk, "to_dict") else repr(risk),
                    "registered_risk": registered.to_dict() if hasattr(registered, "to_dict") else None,
                    "has_config_overrides": "config_overrides" in kwargs,
                    "mode": getattr(kwargs.get("config_overrides"), "mode", None),
                })
            return decision

        policy.check = check

    def uninstall(self) -> None:
        try:
            delattr(self.policy, "check")
        except AttributeError:
            pass


def run_case(label: str, *, tool_name: str, decl: dict[str, Any], with_callback: bool,
             instructions: str, message: str, session_id: str, docstring: bool = True) -> dict[str, Any]:
    root = WORK / label
    provider = ToolCallProvider()
    old_env = chdir_env(root)
    row: dict[str, Any] = {"case": label, "tool_name": tool_name, "decl": {k: list(v) if isinstance(v, tuple) else v
                                                                          for k, v in decl.items()},
                           "approval_callback": "approves" if with_callback else None}
    runtime = rec = None
    calls: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    try:
        fn = make_tool(tool_name, decl, calls, docstring=docstring)
        d = fn.tool_decl
        row["tool_decl"] = {"name": d.name, "min_scope": d.min_scope, "idempotent": d.idempotent,
                            "dangerous": d.dangerous, "tags": list(d.tags), "capabilities": list(d.capabilities)}
        try:
            fam = fn.tool_family_spec()
            row["family_min_scope_default"] = getattr(fam, "min_scope_default", None)
        except Exception as exc:  # noqa: BLE001
            row["family_min_scope_default"] = f"{type(exc).__name__}: {exc}"
        runtime = build_runtime(root, provider)
        action_policy = getattr(runtime, "action_policy", None)
        row["action_policy_present"] = action_policy is not None
        if action_policy is not None:
            rec = PolicyRecorder(action_policy, tool_name)

        def callback(tool_name_arg: str, args: dict[str, Any], approval_id: str) -> bool:
            approvals.append({"tool": tool_name_arg, "args": dict(args or {}), "approval_id_set": bool(approval_id)})
            return True

        kwargs: dict[str, Any] = {"timeout_seconds": 120}
        if with_callback:
            kwargs["approval_callback"] = callback
        provider.armed = tool_name
        try:
            with Agent(instructions=instructions, tools=[fn], session_id=session_id, runtime=runtime) as agent:
                res = agent.run(message, **kwargs)
            row.update({"run_state": res.run_state, "text": res.text,
                        "respond_kind": (res.metadata or {}).get("respond_kind")})
        except Exception as exc:  # noqa: BLE001 - recorded
            row["error"] = f"{type(exc).__name__}: {exc}"
        row["model_called_tool"] = any(r.get("kind") == "tool_call" for r in provider.requests)
        row["provider_requests"] = [r["kind"] for r in provider.requests]
        row["tool_names_offered"] = sorted({n for names in provider.seen_tool_lists for n in names})[:40]
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        row["case_error"] = f"{type(exc).__name__}: {exc}"
        row["case_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                    for f in traceback.extract_tb(exc.__traceback__)][-8:]
    finally:
        row["tool_executions"] = calls
        row["approval_callback_calls"] = approvals
        row["policy_checks"] = rec.rows if rec is not None else []
        if rec is not None:
            rec.uninstall()
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:  # noqa: BLE001
                row["runtime_close"] = f"{type(exc).__name__}: {exc}"
        restore_env(old_env)
        provider.close()
    return row


# ---------------------------------------------------------------- provider-free policy decisions
def offline_decision(decl_label: str, *, mode: str | None = None, default_action: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"decl": decl_label, "mode": mode or "(default)", "default_action": default_action or "(default)"}
    try:
        from openminion.base.config import OpenMinionConfig
        from openminion.modules.policy.runtime.action_policy import build_action_policy_service
        from openminion.modules.tool.framework import build_registrar
        from openminion.modules.tool.registry import ToolRegistry

        fn = make_tool("lookup_order", DECLS[decl_label], [])
        registry = ToolRegistry()
        build_registrar(fn.tool_family_spec()).register(registry)
        config = OpenMinionConfig()
        if mode is not None:
            config.action_policy.mode = mode
        if default_action is not None:
            config.action_policy.default_action = default_action
        out["config_action_policy"] = {"mode": config.action_policy.mode,
                                       "default_action": config.action_policy.default_action,
                                       "allow_read_only_without_prompt": config.action_policy.allow_read_only_without_prompt}
        spec = registry.get("lookup_order")
        out["spec_min_scope"] = getattr(spec, "min_scope", None)
        with tempfile.TemporaryDirectory(dir=str(WORK), ignore_cleanup_errors=True) as tmp:
            policy = build_action_policy_service(config=config, tool_registry=registry, data_root=Path(tmp))
            try:
                d = policy.check({"tool": "lookup_order", "method": "default", "args": dict(ARGS)}, {})
                out.update({"decision": d.decision, "reason_code": d.reason_code, "risk": d.risk.to_dict()})
            finally:
                policy.close()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


offline = {
    f"{label}@default": offline_decision(label) for label in DECLS
}
offline.update({
    f"{label}@ask+allow": offline_decision(label, mode="ask", default_action="allow")
    for label in ("ARTICLE", "BARE", "READ_ONLY")
})

cases = {
    "ARTICLE": run_case("ARTICLE", tool_name="inspect_home", decl=DECLS["ARTICLE"], with_callback=True,
                        instructions="Call inspect_home once, then reply briefly.", message="Investigate example.com.",
                        session_id="probe-article"),
    "BARE": run_case("BARE", tool_name="lookup_order", decl=DECLS["BARE"], with_callback=True,
                     instructions="Answer briefly.", message="Check order A-104", session_id="probe-bare"),
    "READ_ONLY": run_case("READ_ONLY", tool_name="lookup_order", decl=DECLS["READ_ONLY"], with_callback=True,
                          instructions="Answer briefly.", message="Check order A-104", session_id="probe-read-only"),
    "README": run_case("README", tool_name="lookup_order", decl=DECLS["BARE"], with_callback=False,
                       instructions="Answer briefly.", message="Check order A-104", session_id="customer-42",
                       docstring=False),
    "README_FIXED": run_case("README_FIXED", tool_name="lookup_order", decl=DECLS["READ_ONLY"], with_callback=False,
                             instructions="Answer briefly.", message="Check order A-104", session_id="customer-42"),
}


def facts(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in ("run_state", "respond_kind", "text", "error", "case_error", "model_called_tool",
                                    "tool_executions", "approval_callback_calls", "policy_checks")}


def first_check(row: dict[str, Any]) -> dict[str, Any]:
    return (row.get("policy_checks") or [{}])[0]


def ran(row: dict[str, Any]) -> bool:
    return len(row.get("tool_executions") or []) >= 1


def prompted(row: dict[str, Any]) -> bool:
    return PROMPT_MARK.lower() in str(row.get("text") or "").lower() or row.get("respond_kind") == "policy_confirmation_prompt"


# ---------------------------------------------------------------- EXPECT (exit code)
r, rf, ro = cases["README"], cases["README_FIXED"], cases["READ_ONLY"]
expect.check("EXPECT README snippet (bare @tool lookup_order, no approval_callback, default action policy): the model's "
             "lookup_order call runs and the reply is not a policy confirmation prompt",
             r.get("model_called_tool") and ran(r) and not prompted(r), facts(r))
expect.check("EXPECT Control README_FIXED (@tool(min_scope='READ_ONLY', idempotent=True), no callback): policy ALLOW / "
             "READ_ONLY_ALLOW, the tool runs, no confirmation prompt",
             rf.get("model_called_tool") and ran(rf) and not prompted(rf)
             and first_check(rf).get("decision") == "ALLOW" and first_check(rf).get("reason_code") == "READ_ONLY_ALLOW",
             facts(rf))
expect.check("EXPECT Control READ_ONLY (min_scope='READ_ONLY', approval_callback installed): the callback is never asked "
             "and the tool runs once",
             ro.get("model_called_tool") and not ro.get("approval_callback_calls") and len(ro.get("tool_executions") or []) == 1,
             facts(ro))
hosts = sorted({h for row in cases.values() for h in row.get("provider_hosts", [])})
errors = {k: row.get("case_error") or row.get("error") for k, row in cases.items()
          if row.get("case_error") or row.get("error")}
offline_errors = {k: v["error"] for k, v in offline.items() if v.get("error")}
expect.check("EXPECT Offline provider and no case error: every case's openai base URL is the scripted server on 127.0.0.1, "
             "every case's model call reached the tool, and the provider-free policy checks ran",
             hosts == ["127.0.0.1"] and not errors and not offline_errors
             and all(row.get("model_called_tool") for row in cases.values()),
             {"hosts": hosts, "errors": errors, "offline_errors": offline_errors})

# ---------------------------------------------------------------- CLAIM (recorded, no exit-code effect)
a, b = cases["ARTICLE"], cases["BARE"]
claim.check("CLAIM Article observation: @tool(idempotent=True, tags=('read_only',), capabilities=('read_only','network')) "
            "without min_scope reaches approval_callback once (decl min_scope None -> family default WRITE_SAFE)",
            len(a.get("approval_callback_calls") or []) == 1 and (a.get("tool_decl") or {}).get("min_scope") is None
            and a.get("family_min_scope_default") == "WRITE_SAFE", facts(a))
claim.check("CLAIM ARTICLE: first action-policy decision is REQUIRE_CONFIRM / ENFORCE_SAFE_CONFIRM on risk write/local/"
            "reversible with default_confirm False (idempotent -> default_confirm False is not consulted)",
            first_check(a).get("decision") == "REQUIRE_CONFIRM"
            and first_check(a).get("reason_code") == "ENFORCE_SAFE_CONFIRM"
            and (first_check(a).get("risk") or {}).get("risk_class") == "write"
            and (first_check(a).get("risk") or {}).get("reversibility") == "reversible"
            and (first_check(a).get("risk") or {}).get("default_confirm") is False,
            a.get("policy_checks"))
claim.check("CLAIM ARTICLE: after the callback approves, the tool runs once", len(a.get("tool_executions") or []) == 1,
            a.get("tool_executions"))
claim.check("CLAIM BARE @tool with a callback: callback asked once; REQUIRE_CONFIRM / UNKNOWN_REVERSIBILITY on write/"
            "unknown (the #464 classification); the tool runs after approval",
            len(b.get("approval_callback_calls") or []) == 1
            and first_check(b).get("reason_code") == "UNKNOWN_REVERSIBILITY" and ran(b), facts(b))
claim.check("CLAIM README snippet without a callback ends at the policy prompt (respond_kind policy_confirmation_prompt "
            "or 'Policy confirmation required' in the text) and the tool never runs",
            prompted(r) and not ran(r), facts(r))
o = offline
claim.check("CLAIM Provider-free, default OpenMinionConfig (mode auto -> enforce_safe): ARTICLE REQUIRE_CONFIRM/"
            "ENFORCE_SAFE_CONFIRM, BARE REQUIRE_CONFIRM/UNKNOWN_REVERSIBILITY, READ_ONLY ALLOW/READ_ONLY_ALLOW",
            (o["ARTICLE@default"].get("decision"), o["ARTICLE@default"].get("reason_code")) == ("REQUIRE_CONFIRM", "ENFORCE_SAFE_CONFIRM")
            and (o["BARE@default"].get("decision"), o["BARE@default"].get("reason_code")) == ("REQUIRE_CONFIRM", "UNKNOWN_REVERSIBILITY")
            and (o["READ_ONLY@default"].get("decision"), o["READ_ONLY@default"].get("reason_code")) == ("ALLOW", "READ_ONLY_ALLOW"),
            {k: (v.get("decision"), v.get("reason_code")) for k, v in o.items()})
claim.check("CLAIM Provider-free: tags/capabilities 'read_only' change nothing (ARTICLE and ARTICLE_NO_TAGS get the same "
            "decision, reason and risk)",
            all(o["ARTICLE@default"].get(k) == o["ARTICLE_NO_TAGS@default"].get(k) for k in ("decision", "reason_code", "risk")),
            {"ARTICLE": o["ARTICLE@default"], "ARTICLE_NO_TAGS": o["ARTICLE_NO_TAGS@default"]})
claim.check("CLAIM Provider-free: idempotent only matters outside the defaults - with mode 'ask' + default_action 'allow', "
            "ARTICLE is ALLOWed while BARE still needs confirmation (UNKNOWN_REVERSIBILITY)",
            o["ARTICLE@ask+allow"].get("decision") == "ALLOW"
            and (o["BARE@ask+allow"].get("decision"), o["BARE@ask+allow"].get("reason_code")) == ("REQUIRE_CONFIRM", "UNKNOWN_REVERSIBILITY"),
            {k: (v.get("decision"), v.get("reason_code")) for k, v in o.items() if k.endswith("ask+allow")})

summary, claims = expect.summary(), claim.summary()
write_result(OUT, {"info": info, "summary": summary, "all_pass": expect.all_pass, "checks": expect.items,
                   "claims_summary": claims, "claims": claim.items,
                   "observed": {"cases": cases, "offline_policy": offline}})
print("CLAIM results are above as PASS/FAIL lines after the EXPECT lines; they do not change the exit code.")
print(f"CLAIMS matched={claims['passed']} unmatched={claims['failed']} total={claims['total']}")
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if expect.all_pass else 1)
