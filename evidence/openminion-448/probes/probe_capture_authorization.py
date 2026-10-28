"""Direct terminal capture vs. the capture-recovery authorization, with an identity hard constraint loaded.

Installed package only (no repo test fixtures). Each case builds a real
`APIRuntime` (one agent, `openminion`; the SDK's `openai` provider pointed at a
scripted OpenAI-compatible server on 127.0.0.1, no external network) with an
identity profile (`profile.yaml` under the configured `identity.bundle_root`)
and runs ONE unmodified SDK turn, "Please remember that I prefer tabs over
spaces for indentation.", in session `capture`. Nothing is injected: no store
failure, no patched result. The turn's auto fact extraction returns one
`user_preference` candidate and the turn's terminal capture writes its bundle.

  W  hard constraint "NEVER write memory records" (the observation)
  C  hard constraint "NEVER reveal secrets" (control: identity loaded, no write constraint)

For each case the probe records, with call-through wrappers that only record and
then call the original (probe_420_lib.Observers plus one recorder on the
agent's SecurityPolicyEngine.evaluate for every check):
  - the loaded constraint: the profile file, the identity service's profile and
    the SecurityPolicyEngine's identity constraints;
  - the turn: capture state and bundle result, the memctl bundle write, every
    security-policy evaluation during the turn, the provider requests, the
    memory.capture.result event and the memory store rows for the capture;
  - after the turn, two gates for the same agent, asked by the probe:
      * recovery gate: the installed `memory_capture_recovery_allowed` with the
        same arguments the cron executor passes (security policy, tools,
        identity agent id), for this turn's capture identity;
      * tool gate: the turn's own bound tool-policy adapter
        (`runner.tool_api.policy_adapter.evaluate`) for the `memory.write` tool.

Usage: python -I -X utf8 probe_capture_authorization.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import sys
import threading
import time
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


info = banner("probe_capture_authorization",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic")},
              openminion_direct_url=_direct_url())

from openminion import Agent  # noqa: E402
from openminion.modules.policy.adapters.memory import memory_capture_recovery_allowed  # noqa: E402
from openminion.modules.session.capture import build_capture_identity  # noqa: E402
from openminion.services.agent.execution.composition import AgentServiceTurnFlowAdapter  # noqa: E402
from probe_420_lib import (  # noqa: E402
    AGENT_ID,
    CAPTURE_ITEM,
    CAPTURE_MESSAGE,
    Observers,
    ScriptedProvider,
    build_runtime,
    chdir_env,
    intent_view,
    restore_env,
    session_events,
    sqlite_rows,
)

c = Checks()
observed: dict[str, Any] = {}
WRITE_CONSTRAINT = "NEVER write memory records"
NEUTRAL_CONSTRAINT = "NEVER reveal secrets"
SESSION = "capture"


def parse(value: Any) -> dict[str, Any]:
    try:
        return json.loads(value) if isinstance(value, str) else dict(value or {})
    except (TypeError, ValueError):
        return {"unparsed": value}


def check_row(check: Any, decision: Any) -> dict[str, Any]:
    action = check.action
    context = check.context
    return {"resource": action.resource, "verb": action.verb, "tool_name": action.tool_name or None,
            "channel": context.channel, "target": context.target,
            "decision": getattr(decision, "decision", None), "reason_code": getattr(decision, "reason_code", None),
            "allowed": bool(getattr(decision, "allowed", False)),
            "details": {k: v for k, v in dict(getattr(decision, "details", {}) or {}).items()
                        if k in ("identity_constraint_violated", "tool_name", "action")},
            "thread": threading.current_thread().name}


def run_case(name: str, constraint: str) -> dict[str, Any]:
    root = WORK / name
    provider = ScriptedProvider()
    old_env = chdir_env(root)
    row: dict[str, Any] = {"constraint_configured": constraint}
    runtime = obs = None
    policy_undo: Any = None
    try:
        runtime = build_runtime(root, provider, identity_constraint=constraint)
        obs = Observers(runtime).install()
        assert obs.fail_next_capture_writes == 0
        # -------------------------------------------------- the loaded constraint
        profile_path = Path(runtime.config.identity.bundle_root) / AGENT_ID / "profile.yaml"
        identityctl = getattr(obs.service, "_identityctl", None)
        profile = identityctl.get_profile(AGENT_ID) if identityctl is not None else None
        row["identity"] = {
            "config_identity_bundle_root_is_case_dir": Path(runtime.config.identity.bundle_root).resolve()
            == (root / "identity").resolve(),
            "profile_file": profile_path.name if profile_path.is_file() else "missing",
            "profile_file_text": profile_path.read_text(encoding="utf-8") if profile_path.is_file() else None,
            "identityctl": type(identityctl).__name__,
            "identityctl_profile_hard_constraints": list(getattr(getattr(profile, "role", None), "hard_constraints",
                                                                 []) or []),
            "security_policy": type(obs.security_policy).__name__,
            "security_policy_identity_constraints": list(getattr(obs.security_policy, "_identity_constraints",
                                                                 []) or []),
        }
        # -------------------------------------------------- record every security-policy evaluation
        evaluations: list[dict[str, Any]] = []
        policy = obs.security_policy
        inner_evaluate = policy.evaluate  # Observers' call-through recorder, itself wrapping the original

        def evaluate(check: Any, *args: Any, **kwargs: Any) -> Any:
            decision = inner_evaluate(check, *args, **kwargs)
            evaluations.append({"t": time.monotonic(), **check_row(check, decision)})
            return decision

        policy.evaluate = evaluate
        policy_undo = (policy, inner_evaluate)
        # -------------------------------------------------- one unmodified SDK turn
        t0 = time.monotonic()
        provider_before = len(provider.requests)
        with Agent(runtime=runtime) as agent:
            res = agent.run(CAPTURE_MESSAGE, session_id=SESSION, timeout_seconds=120)
        t1 = time.monotonic()
        md = dict(res.metadata or {})
        row["turn"] = {"run_state": res.run_state, "text": res.text,
                       "memory_capture_state": md.get("memory_capture_state"),
                       "memory_capture_bundle_result": parse(md.get("memory_capture_bundle_result"))}
        row["turn_bundle_writes"] = list(obs.bundle_writes)
        row["injected_failures"] = [w.get("injected") for w in obs.bundle_writes if w.get("injected")]
        row["turn_policy_evaluations"] = [{k: v for k, v in e.items() if k != "t"} for e in evaluations
                                          if t0 <= e["t"] <= t1]
        row["turn_provider_requests"] = [r["kind"] for r in provider.requests[provider_before:]]
        intents = session_events(runtime.sessions, "turn.outcome", SESSION)
        row["intents"] = [intent_view(e) for e in intents]
        intent = row["intents"][0] if intents else {}
        capture_id = str(intent.get("capture_id") or "")
        root_turn = str(intent.get("root_turn_id") or "")
        identity = build_capture_identity(runtime_session_id=SESSION, root_turn_id=root_turn) if root_turn else None
        row["capture_id"] = capture_id
        row["root_turn_id"] = root_turn
        row["canonical_identity_matches"] = bool(identity is not None and identity.capture_id == capture_id
                                                 and identity.event_id == intent.get("canonical_event_id"))
        row["results"] = [dict(e.payload) | {"canonical_event_id": e.canonical_event_id}
                          for e in session_events(runtime.sessions, "memory.capture.result")
                          if e.payload.get("capture_id") == capture_id]
        store = obs.assembly.service._store
        inner = getattr(store, "_store", None)
        db = Path(str(inner.db_path)) if inner is not None and getattr(inner, "db_path", None) else None
        row["memory_store"] = {"outer": type(store).__name__, "inner": type(inner).__name__ if inner else None}
        row["memory_rows"] = {
            "candidates": sqlite_rows(db, "SELECT candidate_id, session_id, type, key, status, "
                                          "json_extract(meta_json, '$.capture_id') FROM memory_candidates "
                                          "WHERE json_extract(meta_json, '$.capture_id') = ?", (capture_id,)),
            "receipts": sqlite_rows(db, "SELECT capture_id, disposition, output_ids_json FROM "
                                        "memory_capture_bundle_receipts WHERE capture_id = ?", (capture_id,)),
        } if db is not None else {"store": "not a built-in SQLite memory store"}
        # -------------------------------------------------- the two gates, asked by the probe after the turn
        port = AgentServiceTurnFlowAdapter(obs.service)
        mark = len(evaluations)
        allowed = memory_capture_recovery_allowed(
            SESSION, root_turn, identity.event_id if identity else "", capture_id,
            policy=port.security_policy, tools=port.tools, agent_id=port.identity_agent_id)
        row["recovery_gate"] = {"returned": allowed,
                                "evaluations": [{k: v for k, v in e.items() if k != "t"}
                                                for e in evaluations[mark:]]}
        runner = obs.service._get_runner()
        adapter = getattr(getattr(runner, "tool_api", None), "policy_adapter", None)
        spec = port.tools.get("memory.write") if hasattr(port.tools, "get") else None
        mark = len(evaluations)
        decision = adapter.evaluate(tool_name="memory.write", tool_spec=spec, args={}) if adapter is not None else None
        row["tool_gate"] = {
            "adapter": type(adapter).__name__,
            "adapter_context": {k: getattr(adapter.context, k, None) for k in ("channel", "target")}
            if adapter is not None else None,
            "tool_spec": type(spec).__name__,
            "returned": {"allowed": getattr(decision, "allowed", None), "code": getattr(decision, "code", None),
                         "reason": getattr(decision, "reason", None),
                         "requires_confirm": getattr(decision, "requires_confirm", None)},
            "evaluations": [{k: v for k, v in e.items() if k != "t"} for e in evaluations[mark:]],
        }
        row["provider_requests_total"] = len(provider.requests)
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        import traceback

        row["case_error"] = f"{type(exc).__name__}: {exc}"
        row["case_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                    for f in traceback.extract_tb(exc.__traceback__)][-6:]
    finally:
        if policy_undo is not None:
            policy_undo[0].evaluate = policy_undo[1]
        if obs is not None:
            obs.uninstall()
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:  # noqa: BLE001
                row["runtime_close"] = f"{type(exc).__name__}: {exc}"
        restore_env(old_env)
        provider.close()
    return row


def capture_writes(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [w for w in row.get("turn_bundle_writes", []) if w.get("capture_id") == row.get("capture_id")]


def direct_write_succeeded(row: dict[str, Any]) -> bool:
    turn = row.get("turn", {})
    bundle = turn.get("memory_capture_bundle_result", {})
    writes = capture_writes(row)
    result = (writes[0].get("result") if writes else {}) or {}
    rows = row.get("memory_rows", {})
    return (turn.get("run_state") == "completed" and turn.get("memory_capture_state") == "succeeded"
            and bundle.get("disposition") == "succeeded" and len(bundle.get("output_ids", [])) == 1
            and not bundle.get("error_code") and row.get("injected_failures") == []
            and len(writes) == 1 and "raised" not in writes[0]
            and [{k: i.get(k) for k in ("kind", "normalized_key")} for i in writes[0].get("candidates", [])]
            == [{k: CAPTURE_ITEM[k] for k in ("kind", "normalized_key")}]
            and result.get("disposition") == "succeeded" and result.get("output_ids") == bundle.get("output_ids")
            and len(row.get("results", [])) == 1
            and row["results"][0].get("canonical_event_id") == f"memory.capture.result:{row.get('capture_id')}"
            and isinstance(rows.get("candidates"), list) and len(rows["candidates"]) == 1
            and rows["candidates"][0][0] == bundle.get("output_ids", [None])[0]
            and isinstance(rows.get("receipts"), list) and len(rows["receipts"]) == 1
            and rows["receipts"][0][1] == "succeeded"
            and "extraction" in row.get("turn_provider_requests", []))


def no_memory_check_during_turn(row: dict[str, Any]) -> bool:
    evals = row.get("turn_policy_evaluations", [])
    return (len(evals) > 0 and all(e.get("tool_name") is None for e in evals)
            and not any("memory" in str(e.get(k) or "").lower() or "capture" in str(e.get(k) or "").lower()
                        for e in evals for k in ("resource", "verb", "tool_name", "target")))


w = run_case("W", WRITE_CONSTRAINT)
observed["W_write_constraint"] = w
k = run_case("C", NEUTRAL_CONSTRAINT)
observed["C_neutral_constraint"] = k

for tag, row, constraint in (("W", w, WRITE_CONSTRAINT), ("C", k, NEUTRAL_CONSTRAINT)):
    ident = row.get("identity", {})
    c.check(f"{tag}. the identity profile is loaded: profile.yaml under the configured identity.bundle_root has the hard "
            f"constraint {constraint!r}, the identity service's profile has it and the agent's SecurityPolicyEngine "
            "holds it as its identity constraints",
            ident.get("config_identity_bundle_root_is_case_dir") is True and ident.get("profile_file") == "profile.yaml"
            and constraint in str(ident.get("profile_file_text"))
            and ident.get("identityctl_profile_hard_constraints") == [constraint]
            and ident.get("security_policy") == "SecurityPolicyEngine"
            and ident.get("security_policy_identity_constraints") == [constraint], ident)

c.check("W. unmodified SDK turn with the write constraint loaded: the turn completes and its direct terminal capture "
        "writes the extracted candidate: memory_capture_state succeeded, bundle disposition succeeded with one output "
        "id, one memctl bundle write with the candidate (no injected failure, nothing raised), one "
        "memory.capture.result, one memory_candidates row and one succeeded receipt row; the provider got the turn's "
        "extraction request",
        direct_write_succeeded(w),
        {"turn": w.get("turn"), "writes": capture_writes(w), "results": len(w.get("results", [])),
         "memory_rows": w.get("memory_rows"), "provider": w.get("turn_provider_requests"),
         "injected": w.get("injected_failures")})
c.check("W. during that turn the security policy is evaluated only for the gateway turn and the channel send; no check "
        "names memory.write, a memory resource or capture",
        no_memory_check_during_turn(w)
        and sorted({(e["resource"], e["verb"]) for e in w.get("turn_policy_evaluations", [])})
        == [("channel", "message.send"), ("gateway", "turn.execute")],
        w.get("turn_policy_evaluations"))
rg = w.get("recovery_gate", {})
c.check("W. for the same capture identity, the recovery gate the cron executor uses (memory_capture_recovery_allowed: "
        "tool memory.write, channel cron, target memory-capture-recovery) returns False: require_approval, "
        "identity_hard_constraint_restricted, constraint 'NEVER write memory records'",
        rg.get("returned") is False and len(rg.get("evaluations", [])) == 1
        and rg["evaluations"][0].get("tool_name") == "memory.write" and rg["evaluations"][0].get("channel") == "cron"
        and rg["evaluations"][0].get("target") == "memory-capture-recovery"
        and rg["evaluations"][0].get("decision") == "require_approval"
        and rg["evaluations"][0].get("reason_code") == "identity_hard_constraint_restricted"
        and rg["evaluations"][0].get("details", {}).get("identity_constraint_violated") == WRITE_CONSTRAINT, rg)
tg = w.get("tool_gate", {})
c.check("W. the turn's own tool-policy adapter (channel console, target tool-runtime) does not allow the memory.write "
        "tool: require_approval, identity_hard_constraint_restricted",
        tg.get("adapter") == "ExecutionBoundaryPolicyAdapter" and tg.get("returned", {}).get("allowed") is False
        and tg.get("returned", {}).get("requires_confirm") is True
        and [e.get("reason_code") for e in tg.get("evaluations", [])] == ["identity_hard_constraint_restricted"], tg)
c.check("C. control (identity loaded, constraint 'NEVER reveal secrets'): the same unmodified turn writes the same way "
        "(succeeded, one output, one result, one candidate and receipt row), with the same two policy checks during "
        "the turn",
        direct_write_succeeded(k) and no_memory_check_during_turn(k)
        and sorted({(e["resource"], e["verb"]) for e in k.get("turn_policy_evaluations", [])})
        == [("channel", "message.send"), ("gateway", "turn.execute")],
        {"turn": k.get("turn"), "memory_rows": k.get("memory_rows"), "evals": k.get("turn_policy_evaluations")})
kg, kt = k.get("recovery_gate", {}), k.get("tool_gate", {})
c.check("C. control: the recovery gate returns True (allow) and the tool gate allows memory.write, so the denial in W "
        "comes from the loaded write constraint",
        kg.get("returned") is True and [e.get("decision") for e in kg.get("evaluations", [])] == ["allow"]
        and kt.get("returned", {}).get("allowed") is True, {"recovery_gate": kg, "tool_gate": kt})
hosts = sorted({h for r in (w, k) for h in r.get("provider_hosts", [])})
c.check("Offline provider: each case's openai base URL is the scripted server on 127.0.0.1",
        hosts == ["127.0.0.1"] and all(isinstance(r.get("provider_requests_total"), int)
                                       and r["provider_requests_total"] > 0 for r in (w, k)),
        {"hosts": hosts, "requests": {"W": w.get("provider_requests_total"), "C": k.get("provider_requests_total")}})

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": observed})
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
