"""#496 release verification: a turn's optional `decision` record and `tool_outcome`
candidate follow the turn's `memory.write` answer.

Installed package only (no repo test fixtures); run with `python -I -X utf8`
from an empty directory outside any checkout. Harness: `probe_420_lib.py` (the
#448 probe's helpers, unchanged) and `probe_496_lib.py` (the #496 report
probe's scripted provider and recorders, plus the readers below). Each case is
a real `APIRuntime` for one agent, `openminion`, whose `openai` provider is a
scripted OpenAI-compatible server on 127.0.0.1; the memory store is the
built-in store, on SQLite (`--backend sqlite`) or in its own PostgreSQL schema
(`--backend postgres`, runtime storage stays SQLite).

Cases (each a fresh runtime home):
  W   identity profile `NEVER write memory records` (memory.write: require_approval)
  D   no identity profile; memory.write's `min_scope` set to POWER_USER on the
      runtime's tool registry, as the PR's test does, so the policy denies the
      agent's internal actor (deny, missing_tool_scope)
  WA  as W, but the user approves the model's memory.write call
  N   identity profile `NEVER reveal secrets` (allowed control)
  X   no identity profile (allowed control)
  P   no security policy (OPENMINION_DISABLE_SECURITY_POLICY=true)
Steps: T1 `Agent.run` s1 with the capture message; T2 `Agent(forced_tools=
["memory.write"]).run` s1, the model calls memory_write once and the approval
callback denies it (WA: approves); T3 `APIRuntime.run_turn` s2, conversation
`chat`, "Say hello."; C one memory consolidation run (installed
`task.consolidate_memory` handler, daemon TaskManager and cron scheduler).
  R   one runtime and ONE runner across allow/deny/allow: R1 SDK turn (N
      profile), then the profile is rewritten to W and reloaded
      (`identity_reload`), R2 SDK turn, R3 consolidation run, R4 diagnostic turn
      (the message `openminion agent-check` sends), then back to N: R5 SDK turn,
      R6 consolidation run, R7 diagnostic turn, R8 consolidation run. Each SDK
      turn sends the capture message and forces one memory.write call (denied).
On PostgreSQL the cases are W, N and R.

Usage: python -I -X utf8 probe_496.py <work_dir> <out.json> [--backend sqlite|postgres]
           [--baseline <control.json>] [--expect-version 0.1.16]
The 0.1.15 control runs without --baseline; the 0.1.16 run compares the
unchanged behavior (tool approvals, the allowed controls' records) with it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from importlib import metadata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("work")
ap.add_argument("out")
ap.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
ap.add_argument("--baseline")
ap.add_argument("--expect-version", default="0.1.16")
ARGS = ap.parse_args()
WORK = Path(ARGS.work).resolve()
OUT = Path(ARGS.out).resolve()
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


info = banner("probe_496", backend=ARGS.backend,
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "sqlalchemy", "psycopg")},
              openminion_direct_url=_direct_url(), baseline=Path(ARGS.baseline).name if ARGS.baseline else None)

from openminion import Agent  # noqa: E402
from openminion.base.types import Message  # noqa: E402
from openminion.modules.controlplane.constants import CALLER_HANDLES_DELIVERY_METADATA_KEY  # noqa: E402
from openminion.modules.policy.adapters.memory import memory_capture_recovery_allowed  # noqa: E402
from openminion.modules.session.capture import build_capture_identity  # noqa: E402
from openminion.services.agent.execution.composition import AgentServiceTurnFlowAdapter  # noqa: E402

import probe_496_lib as L  # noqa: E402
from probe_420_lib import (  # noqa: E402
    AGENT_ID,
    CAPTURE_MESSAGE,
    PROFILE_YAML,
    Cron,
    Observers,
    build_runtime,
    chdir_env,
    intent_view,
    restore_env,
    session_events,
)

c = Checks()
DISABLE_POLICY_ENV = "OPENMINION_DISABLE_SECURITY_POLICY"


# ---------------------------------------------------------------- steps
def _marks(ctx: dict[str, Any]) -> dict[str, Any]:
    db: L.MemDB = ctx["db"]
    return {
        "req": len(ctx["provider"].requests), "writes": len(ctx["rec"].writes),
        "decisions": len(ctx["rec"].decisions), "dcalls": len(ctx["drec"].calls),
        "devents": len(ctx["drec"].events), "turns": len(ctx["rrec"].rows),
        "recoveries": len(ctx["obs"].recoveries), "extractor": len(ctx["obs"].extractor_calls),
        "events": L.event_type_counts(ctx["runtime"].sessions),
        "records": {r.get("id") for r in db.records()},
        "candidates": {r.get("candidate_id") for r in db.candidates()},
        "t0": time.monotonic(),
    }


def _window(ctx: dict[str, Any], m: dict[str, Any]) -> dict[str, Any]:
    db: L.MemDB = ctx["db"]
    provider: L.TurnProvider = ctx["provider"]
    records, candidates = db.records(), db.candidates()
    end = len(provider.requests)
    calls = L.strip_t(ctx["drec"].calls[m["dcalls"]:])
    for call in calls:
        call["persisted_working_state_after"] = L.working_state(ctx["runner"], call.get("session_id"))
    return {
        "provider_requests": provider.kinds(m["req"], end),
        "request_range": [m["req"], end],
        "memory_write_decisions": [{k: v for k, v in d.items() if k not in ("t", "thread")}
                                   for d in ctx["rec"].decisions[m["decisions"]:]],
        "memory_api_writes": [{k: v for k, v in w.items() if k != "t"} for w in ctx["rec"].writes[m["writes"]:]],
        "decision_memory_calls": calls,
        "decision_memory_events": L.strip_t(ctx["drec"].events[m["devents"]:]),
        "turn_runners": L.strip_t(ctx["rrec"].rows[m["turns"]:]),
        "recovery": [r.get("result") for r in ctx["obs"].recoveries[m["recoveries"]:]],
        "extractor_calls": len(ctx["obs"].extractor_calls) - m["extractor"],
        "new_session_events": L.counts_diff(m["events"], L.event_type_counts(ctx["runtime"].sessions)),
        "new_records": [r for r in records if r.get("id") not in m["records"]],
        "new_candidates": [r for r in candidates if r.get("candidate_id") not in m["candidates"]],
    }


def sdk_turn(ctx: dict[str, Any], label: str, message: str, session_id: str, *, forced: bool = False,
             approve: bool = False, conversation_id: str | None = None) -> dict[str, Any]:
    provider, runtime = ctx["provider"], ctx["runtime"]
    m = _marks(ctx)
    approvals: list[dict[str, Any]] = []

    def approval(*a: Any) -> bool:
        approvals.append({"tool": str(a[0]) if a else None, "args": a[1] if len(a) > 1 else None})
        return approve

    if forced:
        provider.write_tool_call = True
    try:
        if conversation_id is None:
            entry = "Agent.run"
            with Agent(runtime=runtime, forced_tools=["memory.write"] if forced else None) as agent:
                res = agent.run(message, session_id=session_id, timeout_seconds=120, approval_callback=approval)
            run_state, text, md = res.run_state, res.text, dict(res.metadata or {})
        else:
            entry = "APIRuntime.run_turn"
            raw = runtime.run_turn(payload={"message": message, "session_id": session_id, "deliver": False,
                                            "inbound_metadata": {CALLER_HANDLES_DELIVERY_METADATA_KEY: "true"},
                                            "conversation_id": conversation_id, "timeout_seconds": 120},
                                   approval_callback=approval)
            run_state, text, md = raw.get("run_state"), raw.get("body"), dict(raw.get("metadata") or {})
    finally:
        provider.write_tool_call = False
    intents = [intent_view(e) for e in session_events(runtime.sessions, "turn.outcome", session_id)]
    return {"label": label, "kind": "sdk", "entry": entry, "session_id": session_id,
            "conversation_id": conversation_id, "message": message, "forced_memory_write": forced,
            "run_state": run_state, "text": text,
            "memory_capture_state": md.get("memory_capture_state"),
            "memory_capture_bundle_result": L._parse(md.get("memory_capture_bundle_result")),
            "intent": intents[-1] if intents else None,
            "approval_callback_calls": approvals, **_window(ctx, m)}


def diagnostic_turn(ctx: dict[str, Any], label: str) -> dict[str, Any]:
    """The message `openminion agent-check` builds (cli/commands/agent/check.py), on the same service."""
    m = _marks(ctx)
    rid = f"probe496{label.lower()}"
    inbound = Message(channel="console", target="api-user", body=CAPTURE_MESSAGE,
                      metadata={"session_id": f"runtime:agent-check:{rid}", "request_id": rid, "turn_id": rid,
                                "invocation_scope": "runtime", "diagnostic_scope": "runtime"})
    response = asyncio.run(ctx["service"].run_turn(inbound))
    return {"label": label, "kind": "diagnostic", "session_id": f"runtime:agent-check:{rid}",
            "text": getattr(response, "text", None), **_window(ctx, m)}


def consolidation_run(ctx: dict[str, Any], label: str) -> dict[str, Any]:
    cron, obs = ctx["cron"], ctx["obs"]
    m = _marks(ctx)
    queued = cron.run_now(ctx["job_id"])
    done = cron.wait(ctx["job_id"], str(queued.get("run_id")))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not any(
            e.get("event") == "cron.run.finished" and e.get("run_id") == queued.get("run_id")
            for e in obs.cron_events):
        time.sleep(0.05)
    return {"label": label, "kind": "consolidation", "state": done.get("state"), "error": done.get("error"),
            **_window(ctx, m)}


def set_profile(root: Path, constraint: str) -> dict[str, Any]:
    path = root / "identity" / AGENT_ID / "profile.yaml"
    path.write_text(PROFILE_YAML.format(constraint=constraint), encoding="utf-8")
    return {"constraint": constraint}


def capture_facts(ctx: dict[str, Any], capture_id: str | None) -> dict[str, Any]:
    if not capture_id:
        return {"capture_id": None}
    results = [e for e in session_events(ctx["runtime"].sessions, "memory.capture.result")
               if (e.payload or {}).get("capture_id") == capture_id]
    return {"capture_id": capture_id, "memory_capture_result_events": len(results),
            "receipts": ctx["db"].receipts(capture_id), "rows_with_capture_id": ctx["db"].capture_rows(capture_id)}


# ---------------------------------------------------------------- cases
def run_case(label: str, constraint: str | None, *, approve: bool = False, deny_scope: bool = False,
             no_policy: bool = False, reused: bool = False) -> dict[str, Any]:
    root = WORK / label
    provider = L.TurnProvider()
    old_env = chdir_env(root)
    old_policy_env = os.environ.get(DISABLE_POLICY_ENV)
    if no_policy:
        os.environ[DISABLE_POLICY_ENV] = "true"
    row: dict[str, Any] = {"case": label, "constraint": constraint, "approve_model_call": approve,
                           "deny_scope": deny_scope, "no_policy": no_policy, "backend": ARGS.backend}
    runtime = obs = rec = drec = rrec = cron = pg = None
    try:
        if ARGS.backend == "postgres":
            pg = L.PgSchema(label)
        runtime = build_runtime(root, provider, identity_constraint=constraint,
                                memory_config=pg.memory_config if pg is not None else None)
        obs = Observers(runtime).install()
        service = obs.service
        runner = service._get_runner()
        row["security_policy_present"] = obs.security_policy is not None
        row["identity_constraints"] = list(getattr(obs.security_policy, "_identity_constraints", []) or [])
        row["store_chain"] = L.store_chain(obs)
        row["runner_memory_api_is_assembly_memctl"] = runner.memory_api is obs.memctl
        if deny_scope:
            spec = runtime.tools.get("memory.write")
            before = runtime.tools.policy_for("memory.write")
            spec.min_scope = "POWER_USER"
            after = runtime.tools.policy_for("memory.write")
            row["deny_scope_setup"] = {"required_scopes_before": sorted(before.required_scopes_all),
                                       "required_scopes_after": sorted(after.required_scopes_all)}
        rec = L.Recorder(runner.memory_api, obs.security_policy)
        drec = L.DecisionMemoryRecorder()
        rrec = L.RunnerTurnRecorder(service)
        db = L.MemDB(L.memory_db_path(obs), pg)
        row["memory_db"] = db.sqlite_path.name if db.sqlite_path is not None else f"postgres schema {pg.schema}"
        ctx: dict[str, Any] = {"provider": provider, "rec": rec, "drec": drec, "rrec": rrec, "obs": obs,
                               "runtime": runtime, "service": service, "runner": runner, "db": db}
        cron = ctx["cron"] = Cron(runtime, root)
        created = cron.create_consolidation_job(batch_limit=8)
        ctx["job_id"] = row["job_id"] = str(created.get("task_id"))
        cron.start()
        steps: dict[str, Any] = {}
        row["steps"] = steps
        if not reused:
            steps["T1"] = sdk_turn(ctx, "T1", CAPTURE_MESSAGE, "s1")
            intent = steps["T1"]["intent"] or {}
            identity = build_capture_identity(runtime_session_id="s1", root_turn_id=str(intent.get("root_turn_id")))
            port = AgentServiceTurnFlowAdapter(service)
            mark_d = len(rec.decisions)
            row["recovery_gate_T1"] = {
                "returned": memory_capture_recovery_allowed(
                    "s1", identity.root_turn_id, identity.event_id, identity.capture_id,
                    policy=port.security_policy, tools=port.tools, agent_id=port.identity_agent_id),
                "decisions": [{k: v for k, v in d.items() if k not in ("t", "thread")}
                              for d in rec.decisions[mark_d:]]}
            try:
                decision = runner.tool_api.policy_adapter.evaluate(
                    tool_name="memory.write", tool_spec=runtime.tools.get("memory.write"), args={})
                row["tool_adapter_memory_write"] = {
                    "allowed": bool(decision.allowed),
                    "requires_confirm": bool(getattr(decision, "requires_confirm", False)),
                    "reason": getattr(decision, "reason", None)}
            except Exception as exc:  # noqa: BLE001
                row["tool_adapter_memory_write"] = {"error": L.short_error(exc)}
            steps["T2"] = sdk_turn(ctx, "T2", L.QUESTION, "s1", forced=True, approve=approve)
            steps["T3"] = sdk_turn(ctx, "T3", L.HELLO, "s2", conversation_id="chat")
            steps["C"] = consolidation_run(ctx, "C")
            row["capture_T1"] = capture_facts(ctx, (steps["T1"].get("intent") or {}).get("capture_id"))
        else:
            steps["R1"] = sdk_turn(ctx, "R1", CAPTURE_MESSAGE, "r1", forced=True)
            row["profile_switch_1"] = set_profile(root, L.WRITE_CONSTRAINT)
            service.identity_reload()
            row["identity_constraints_after_switch_1"] = list(
                getattr(obs.security_policy, "_identity_constraints", []) or [])
            steps["R2"] = sdk_turn(ctx, "R2", CAPTURE_MESSAGE, "r2", forced=True)
            steps["R3"] = consolidation_run(ctx, "R3")
            steps["R4"] = diagnostic_turn(ctx, "R4")
            row["profile_switch_2"] = set_profile(root, L.NEUTRAL_CONSTRAINT)
            service.identity_reload()
            row["identity_constraints_after_switch_2"] = list(
                getattr(obs.security_policy, "_identity_constraints", []) or [])
            steps["R5"] = sdk_turn(ctx, "R5", CAPTURE_MESSAGE, "r5", forced=True)
            steps["R6"] = consolidation_run(ctx, "R6")
            steps["R7"] = diagnostic_turn(ctx, "R7")
            steps["R8"] = consolidation_run(ctx, "R8")
            row["service_runner_id_at_end"] = id(service._get_runner())
            row["runner_id_at_start"] = id(runner)
            row["capture_R2"] = capture_facts(ctx, (steps["R2"].get("intent") or {}).get("capture_id"))
            row["capture_R1"] = capture_facts(ctx, (steps["R1"].get("intent") or {}).get("capture_id"))
        row["final_records"] = [{k: r.get(k) for k in ("id", "scope", "type", "key", "title")} for r in db.records()]
        row["final_candidates"] = [{k: r.get(k) for k in ("candidate_id", "proposed_scope", "type", "title", "status",
                                                          "content_json")} for r in db.candidates()]
        row["memory_store_tables"] = db.tables()
        row["refused_text_in_memory_store"] = db.search(L.REFUSED_NEEDLES)
        row["refused_text_in_other_sqlite_files"] = L.other_sqlite_hits(root, db.sqlite_path, L.REFUSED_NEEDLES)
        row["all_outcome_feedback_calls"] = [w for w in rec.writes if w["method"] == "apply_outcome_feedback"]
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
        row["provider_request_count"] = len(provider.requests)
        if pg is not None:
            row["postgres_server_side"] = {
                "decision_records": db.rows("SELECT COUNT(*) FROM memory_records WHERE type = 'decision'"),
                "tool_outcome_candidates": db.rows("SELECT COUNT(*) FROM memory_candidates WHERE type = 'tool_outcome'"),
                "all_records": db.rows("SELECT COUNT(*) FROM memory_records"),
                "all_candidates": db.rows("SELECT COUNT(*) FROM memory_candidates"),
                "server_version": db.rows("SHOW server_version"),
                "connections_to_database": db.rows(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"),
            }
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        row["case_error"] = L.short_error(exc)
        row["case_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                    for f in traceback.extract_tb(exc.__traceback__)][-10:]
    finally:
        if cron is not None:
            try:
                cron.stop()
            except Exception as exc:  # noqa: BLE001
                row["scheduler_stop"] = L.short_error(exc)
        for r in (rrec, drec, rec, obs):
            if r is not None:
                try:
                    r.uninstall()
                except Exception as exc:  # noqa: BLE001
                    row.setdefault("uninstall_errors", []).append(L.short_error(exc))
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:  # noqa: BLE001
                row["runtime_close"] = L.short_error(exc)
        if pg is not None:
            try:
                pg.drop()
            except Exception as exc:  # noqa: BLE001
                row["pg_drop"] = L.short_error(exc)
        if old_policy_env is None:
            os.environ.pop(DISABLE_POLICY_ENV, None)
        else:
            os.environ[DISABLE_POLICY_ENV] = old_policy_env
        restore_env(old_env)
        provider.close()
    return row


if ARGS.backend == "sqlite":
    PLAN = {"W": dict(constraint=L.WRITE_CONSTRAINT), "D": dict(constraint=None, deny_scope=True),
            "WA": dict(constraint=L.WRITE_CONSTRAINT, approve=True), "N": dict(constraint=L.NEUTRAL_CONSTRAINT),
            "X": dict(constraint=None), "P": dict(constraint=None, no_policy=True),
            "R": dict(constraint=L.NEUTRAL_CONSTRAINT, reused=True)}
else:
    PLAN = {"W": dict(constraint=L.WRITE_CONSTRAINT), "N": dict(constraint=L.NEUTRAL_CONSTRAINT),
            "R": dict(constraint=L.NEUTRAL_CONSTRAINT, reused=True)}
cases: dict[str, Any] = {}
for name, kwargs in PLAN.items():
    t0 = time.monotonic()
    cases[name] = run_case(name, **kwargs)
    cases[name]["seconds"] = round(time.monotonic() - t0, 1)
    print(f"case {name}: {cases[name]['seconds']} s, error={cases[name].get('case_error')}", flush=True)
baseline = json.loads(Path(ARGS.baseline).read_text(encoding="utf-8")) if ARGS.baseline else None
observed: dict[str, Any] = {"cases": cases}


# ---------------------------------------------------------------- views
def steps_of(case: dict[str, Any]) -> dict[str, Any]:
    return case.get("steps") or {}


def capture_targets(step: dict[str, Any]) -> list[list[str]]:
    return [[d.get("decision"), d.get("reason_code")] for d in step.get("memory_write_decisions", [])
            if d.get("target") == "memory-capture"]


def other_targets(step: dict[str, Any]) -> list[str]:
    return sorted({str(d.get("target")) for d in step.get("memory_write_decisions", [])
                   if d.get("target") not in ("memory-capture", "memory-capture-recovery")})


def decision_writes(step: dict[str, Any]) -> list[dict[str, Any]]:
    return [w for w in step.get("memory_api_writes", [])
            if w.get("method") == "write_record" and w.get("record_type") == "decision"]


def stage_calls(step: dict[str, Any]) -> list[dict[str, Any]]:
    return [w for w in step.get("memory_api_writes", []) if w.get("method") == "stage_candidate"]


def new_decisions(step: dict[str, Any]) -> list[str]:
    return [str(r.get("title")) for r in step.get("new_records", []) if r.get("type") == "decision"]


def new_tool_outcomes(step: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in step.get("new_candidates", []) if r.get("type") == "tool_outcome"]


def dm_events(step: dict[str, Any]) -> list[list[Any]]:
    return [[e.get("event"), e.get("reason")] for e in step.get("decision_memory_events", [])]


def approval_events(step: dict[str, Any]) -> dict[str, int]:
    return {k: v for k, v in (step.get("new_session_events") or {}).items()
            if "approval" in k.lower() or "confirm" in k.lower()}


def step_view(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_state": step.get("run_state", step.get("state")), "text": step.get("text"),
        "memory_capture_decisions": capture_targets(step), "other_memory_write_targets": other_targets(step),
        "decision_memory_events": dm_events(step),
        "decision_write_record_calls": len(decision_writes(step)),
        "new_decision_records": new_decisions(step),
        "stage_candidate_calls": [" <- ".join(w.get("callers", [])[:2]) for w in stage_calls(step)],
        "new_tool_outcome_candidates": [{k: r.get(k) for k in ("proposed_scope", "status", "title")}
                                        | {"outcome": (r.get("content_json") or {}).get("outcome"),
                                           "args_signature": (r.get("content_json") or {}).get("args_signature")}
                                        for r in new_tool_outcomes(step)],
        "approval_callback_calls": step.get("approval_callback_calls"),
        "approval_session_events": approval_events(step),
        "extraction_requests": step.get("provider_requests", []).count("extraction"),
        "recovery": step.get("recovery"),
        "turn_runner_flags": [{k: v for k, v in r.items() if k not in ("runner_id", "body")}
                              for r in step.get("turn_runners", [])],
    }


def tool_outcome_ok(cand: dict[str, Any]) -> bool:
    content, meta = cand.get("content_json") or {}, cand.get("meta_json") or {}
    return (cand.get("proposed_scope") == f"agent:{AGENT_ID}" and cand.get("status") == "proposed"
            and cand.get("title") == L.TOOL_OUTCOME_TITLE and content.get("outcome") == "policy_denied"
            and content.get("args_signature") == L.CANONICAL_ARGS
            and meta.get("source_args_signature") == L.CANONICAL_ARGS)


def store_hits(case: dict[str, Any]) -> dict[str, Any]:
    return case.get("refused_text_in_memory_store") or {}


def no_hits(case: dict[str, Any]) -> bool:
    hits = store_hits(case)
    return bool(hits) and all(v == {} for v in hits.values())


def final_types(case: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in case.get("final_records") or []:
        out[f"record:{r.get('type')}"] = out.get(f"record:{r.get('type')}", 0) + 1
    for r in case.get("final_candidates") or []:
        out[f"candidate:{r.get('type')}"] = out.get(f"candidate:{r.get('type')}", 0) + 1
    return out


def fact_records(case: dict[str, Any]) -> list[str]:
    return [str(r.get("id")) for r in case.get("final_records") or [] if r.get("key") == L.TOOL_ARGS["key"]]


def ws_after(step: dict[str, Any]) -> list[dict[str, Any] | None]:
    return [call.get("persisted_working_state_after") for call in step.get("decision_memory_calls", [])]


def dm_call_view(step: dict[str, Any]) -> list[dict[str, Any]]:
    return [{k: call.get(k) for k in ("decision_route", "decision_reason_code", "state_decision_reason_code",
                                      "runner_memory_write_allowed_for_turn", "returned")}
            for call in step.get("decision_memory_calls", [])]


TURN_STEPS = ("T1", "T2", "T3")
ALL_STEPS = ("T1", "T2", "T3", "C")
REPLIES = {"T1": "openminion: Noted: tabs over spaces.", "T3": "openminion: Hello."}


def denied_case_facts(case: dict[str, Any]) -> dict[str, Any]:
    st = steps_of(case)
    return {
        "case_error": case.get("case_error"),
        "per_step": {s: step_view(st.get(s) or {}) for s in ALL_STEPS},
        "final_store_types": final_types(case),
        "refused_text_in_memory_store": store_hits(case),
        "refused_text_in_other_sqlite_files (not judged)": case.get("refused_text_in_other_sqlite_files"),
        "store_chain": case.get("store_chain"), "memory_db": case.get("memory_db"),
        "postgres_server_side": case.get("postgres_server_side"),
    }


def denied_case_ok(case: dict[str, Any], expected: list[str]) -> bool:
    st = steps_of(case)
    if case.get("case_error") or not all(s in st for s in ALL_STEPS):
        return False
    for s in ALL_STEPS:
        step = st[s]
        events = dm_events(step)
        # the turn (or the consolidation run's scheduled turn) asks memory.write once, with this answer
        if capture_targets(step) != [expected]:
            return False
        if other_targets(step):
            return False
        if decision_writes(step) or new_decisions(step) or stage_calls(step) or new_tool_outcomes(step):
            return False
        if not events or any(e != ["brain.decision_memory.skipped", "memory_write_not_authorized"] for e in events):
            return False
        if any(call.get("returned") != [] for call in step.get("decision_memory_calls", [])):
            return False
    types = final_types(case)
    return types.get("record:decision", 0) == 0 and types.get("candidate:tool_outcome", 0) == 0 and no_hits(case)


MODEL_CALL_PROMPT = {"policy_confirmation_prompt": 1}


def approvals_ok(case: dict[str, Any]) -> bool:
    """Only the model's own memory.write call in T2 asks: the callback once, with at most its one
    `policy_confirmation_prompt` session event; nothing in the other steps; no pending policy approval."""
    st = steps_of(case)
    return ([st.get(s, {}).get("approval_callback_calls") for s in TURN_STEPS]
            == [[], [{"tool": "memory.write", "args": L.TOOL_ARGS}], []]
            and approval_events(st.get("T2") or {}) in ({}, MODEL_CALL_PROMPT)
            and all(not approval_events(st.get(s) or {}) for s in ("T1", "T3", "C"))
            and all((ws or {}).get("pending_policy_approval_id") in (None, "")
                    for s in ALL_STEPS for ws in ws_after(st.get(s) or {})))


def replies_view(case: dict[str, Any]) -> dict[str, Any]:
    st = steps_of(case)
    return {s: [st.get(s, {}).get("run_state"), st.get(s, {}).get("text")] for s in TURN_STEPS}


# ---------------------------------------------------------------- item 5 first (environment), then 1-4
hosts = sorted({h for case in cases.values() for h in case.get("provider_hosts", [])})
errors = {k: case.get("case_error") for k, case in cases.items() if case.get("case_error")}
chains = {k: case.get("store_chain") for k, case in cases.items()}
expected_store = "PostgresMemoryStore" if ARGS.backend == "postgres" else "SQLiteMemoryStore"
c.check(f"item 5: installed package and environment recorded: openminion {ARGS.expect_version} imported from "
        "site-packages in isolated mode; every case's memory store is the built-in "
        f"{expected_store} ({ARGS.backend}); the provider is the scripted server on 127.0.0.1; no case error",
        info.get("openminion.__version__") == ARGS.expect_version and info.get("imported_from_site_packages") is True
        and info.get("isolated_mode") is True and hosts == ["127.0.0.1"] and not errors
        and all(expected_store in (ch or []) for ch in chains.values()),
        {"version": info.get("openminion.__version__"), "file": info.get("openminion.__file__"),
         "python": info.get("python"), "platform": info.get("platform"), "sqlite": info.get("sqlite3.sqlite_version"),
         "dist_versions": info.get("dist_versions"), "store_chains": chains,
         "memory_db": {k: case.get("memory_db") for k, case in cases.items()},
         "postgres_server_version": {k: (case.get("postgres_server_side") or {}).get("server_version")
                                     for k, case in cases.items() if case.get("postgres_server_side")},
         "hosts": hosts, "errors": errors})

W, N, R = cases.get("W", {}), cases.get("N", {}), cases.get("R", {})
RA = ["require_approval", "identity_hard_constraint_restricted"]
ALLOW = ["allow", "allowed"]
DENY = ["deny", "missing_tool_scope"]

# item 1 ---------------------------------------------------------------------------------------------------
rg = W.get("recovery_gate_T1") or {}
c.check("item 1: W setup: the identity profile `NEVER write memory records` is loaded; memory.write is refused for "
        "the agent (recovery's gate for T1's capture: False, require_approval / identity_hard_constraint_restricted; "
        "the turn's tool adapter: not allowed, requires confirmation)",
        W.get("identity_constraints") == [L.WRITE_CONSTRAINT] and rg.get("returned") is False
        and [[d.get("decision"), d.get("reason_code")] for d in rg.get("decisions", [])] == [RA]
        and (W.get("tool_adapter_memory_write") or {}).get("allowed") is False,
        {"constraints": W.get("identity_constraints"), "recovery_gate_T1": rg,
         "tool_adapter": W.get("tool_adapter_memory_write")})
c.check("item 1: W (confirmation-required policy): in T1, T2, T3 and the consolidation run's scheduled turn the turn "
        "asks memory.write once (target memory-capture: require_approval); every decision-record attempt is skipped "
        "(brain.decision_memory.skipped, memory_write_not_authorized) with no write_record call; no stage_candidate "
        "call and no tool_outcome candidate; at the end the memory store holds 0 decision records and 0 tool_outcome "
        "candidates, and no table of the store holds the refused call's text ('The user prefers tabs.', "
        "'fact:indentation')",
        denied_case_ok(W, RA), denied_case_facts(W))
c.check("item 1: W: no new approval request: the approval callback is asked only for the model's own memory.write "
        "call in T2 (once, with its arguments, denied), whose only approval/confirmation session event is that call's "
        "own policy_confirmation_prompt; none in T1, T3 or the consolidation run; no pending policy approval in the "
        "persisted working state; the replies are the same as in the allowed control N",
        approvals_ok(W) and replies_view(W) == replies_view(N) and replies_view(W)["T1"] == ["completed", REPLIES["T1"]]
        and replies_view(W)["T3"] == ["completed", REPLIES["T3"]],
        {"approvals": {s: steps_of(W).get(s, {}).get("approval_callback_calls") for s in TURN_STEPS},
         "approval_session_events": {s: approval_events(steps_of(W).get(s) or {}) for s in ALL_STEPS},
         "pending_policy_approval_id": {s: [(ws or {}).get("pending_policy_approval_id")
                                            for ws in ws_after(steps_of(W).get(s) or {})] for s in ALL_STEPS},
         "replies_W": replies_view(W), "replies_N": replies_view(N)})
if "D" in cases:
    D = cases["D"]
    c.check("item 1: D (denying policy: memory.write's min_scope POWER_USER, so the policy denies the agent's internal "
            "actor): every step's memory.write answer is deny / missing_tool_scope; no decision record, no "
            "stage_candidate call, no tool_outcome candidate, 0 decision records and 0 tool_outcome candidates at the "
            "end, and no table of the memory store holds the refused call's text",
            (D.get("deny_scope_setup") or {}).get("required_scopes_after") == ["tool.execute", "tool.execute.elevated"]
            and denied_case_ok(D, DENY),
            {"setup": D.get("deny_scope_setup"), **denied_case_facts(D)})
    c.check("item 1: D: no new approval request (the callback is asked only for the model's memory.write call in T2, "
            "once, denied, with only that call's policy_confirmation_prompt; nothing in the other steps; no pending "
            "policy approval); replies as in N",
            approvals_ok(D) and replies_view(D) == replies_view(N),
            {"approvals": {s: steps_of(D).get(s, {}).get("approval_callback_calls") for s in TURN_STEPS},
             "approval_session_events": {s: approval_events(steps_of(D).get(s) or {}) for s in ALL_STEPS},
             "replies_D": replies_view(D)})

if ARGS.backend == "postgres":
    def server(case: dict[str, Any]) -> dict[str, Any]:
        side = case.get("postgres_server_side") or {}
        val = lambda k: (side.get(k) or [[None]])[0][0] if isinstance(side.get(k), list) else side.get(k)  # noqa: E731
        return {"decision_records": val("decision_records"), "tool_outcome_candidates": val("tool_outcome_candidates"),
                "all_records": val("all_records"), "all_candidates": val("all_candidates"),
                "tables": case.get("memory_store_tables"), "refused_text": store_hits(case)}

    sw, sn = server(W), server(N)
    memory_tables = {"memory_records", "memory_candidates", "memory_capture_bundle_receipts"}
    c.check("item 1: PostgreSQL, counted server-side in each case's own schema: W has 0 decision records, 0 tool_outcome "
            "candidates and no row in any table holding the refused text, while the allowed control N on the same "
            "server has 4 decision records and 1 tool_outcome candidate holding it; both schemas carry the memory "
            "store's tables",
            sw["decision_records"] == 0 and sw["tool_outcome_candidates"] == 0 and no_hits(W)
            and sn["decision_records"] == 4 and sn["tool_outcome_candidates"] == 1
            and all(v for v in store_hits(N).values())
            and all(memory_tables <= set(x["tables"] or []) for x in (sw, sn)),
            {"W": sw, "N": sn})


# item 2 ---------------------------------------------------------------------------------------------------
def allowed_case_ok(case: dict[str, Any], expected: list[str] | None) -> bool:
    st = steps_of(case)
    if case.get("case_error") or not all(s in st for s in ALL_STEPS):
        return False
    for s in ALL_STEPS:
        step = st[s]
        if len(new_decisions(step)) != 1 or len(decision_writes(step)) != 1:
            return False
        if not new_decisions(step)[0].startswith("Decision: "):
            return False
        if ["brain.decision_memory.completed", None] not in dm_events(step):
            return False
        if any(e[0] == "brain.decision_memory.skipped" for e in dm_events(step)):
            return False
    t2 = new_tool_outcomes(st["T2"])
    if len(t2) != 1 or not tool_outcome_ok(t2[0]):
        return False
    calls = stage_calls(st["T2"])
    if len(calls) != 1 or "_stage_tool_outcome_candidate" not in " ".join(calls[0].get("callers", [])):
        return False
    if any(new_tool_outcomes(st[s]) for s in ("T1", "T3", "C")):
        return False
    return approvals_ok(case) and not fact_records(case)


def allowed_case_facts(case: dict[str, Any]) -> dict[str, Any]:
    st = steps_of(case)
    t2 = new_tool_outcomes(st.get("T2") or {})
    return {"case_error": case.get("case_error"), "per_step": {s: step_view(st.get(s) or {}) for s in ALL_STEPS},
            "T2_tool_outcome_candidate": [{k: r.get(k) for k in ("proposed_scope", "status", "title", "content_json",
                                                                 "meta_json")} for r in t2],
            "final_store_types": final_types(case), "fact_records": fact_records(case),
            "security_policy_present": case.get("security_policy_present"),
            "identity_constraints": case.get("identity_constraints"),
            "postgres_server_side": case.get("postgres_server_side")}


def shape(case: dict[str, Any]) -> dict[str, Any]:
    st = steps_of(case)
    t2 = new_tool_outcomes(st.get("T2") or {})
    return {"decision_titles": {s: new_decisions(st.get(s) or {}) for s in ALL_STEPS},
            "tool_outcome": [{"scope": r.get("proposed_scope"), "status": r.get("status"), "title": r.get("title"),
                              "content": {k: v for k, v in (r.get("content_json") or {}).items()
                                          if k != "turn_index"}} for r in t2]}


for label, desc, expected in (("N", "allowed policy (`NEVER reveal secrets`)", ALLOW),
                              ("X", "no identity profile", ALLOW), ("P", "no security policy", None)):
    if label not in cases:
        continue
    case = cases[label]
    c.check(f"item 2: {label} control, {desc}: each of T1, T2, T3 and the consolidation turn writes one decision record "
            "(write_record from write_decision_memory, brain.decision_memory.completed); T2's refused memory.write call "
            "stages one tool_outcome candidate from _stage_tool_outcome_candidate (agent:openminion, proposed, "
            "policy_denied, args_signature = the refused arguments); the callback is asked once, for the model's call",
            allowed_case_ok(case, expected), allowed_case_facts(case))
    if label == "P":
        decisions = [d for s in ALL_STEPS for d in (steps_of(case).get(s) or {}).get("memory_write_decisions", [])]
        c.check("item 2: P: no security policy is built (OPENMINION_DISABLE_SECURITY_POLICY=true), and no memory.write "
                "decision is asked", case.get("security_policy_present") is False and decisions == [],
                {"security_policy_present": case.get("security_policy_present"), "decisions": decisions})
    else:
        c.check(f"item 2: {label}: the turn's memory.write answer is allow in every step (memory-capture: allow / "
                "allowed, once per turn)",
                all(capture_targets(steps_of(case).get(s) or {}) == [ALLOW] for s in ALL_STEPS),
                {s: capture_targets(steps_of(case).get(s) or {}) for s in ALL_STEPS})
if all(k in cases for k in ("N", "X", "P")):
    c.check("item 2: the three controls land the same optional records (decision titles per step and the T2 "
            "tool_outcome candidate)", shape(cases["N"]) == shape(cases["X"]) == shape(cases["P"]),
            {"N": shape(cases["N"]), "X": shape(cases["X"]), "P": shape(cases["P"])})
if "WA" in cases:
    WA = cases["WA"]
    st = steps_of(WA)
    c.check("item 2: ordinary tool approval unchanged under W: when the user approves the model's memory.write call "
            "(case WA) the approval callback is asked once with its arguments and the approved fact record is "
            "written (key fact:indentation); the turn's optional writes are still skipped (0 decision records, no "
            "tool_outcome candidate)",
            not WA.get("case_error") and st.get("T2", {}).get("approval_callback_calls")
            == [{"tool": "memory.write", "args": L.TOOL_ARGS}]
            and len(fact_records(WA)) == 1 and final_types(WA).get("record:decision", 0) == 0
            and final_types(WA).get("candidate:tool_outcome", 0) == 0
            and all(capture_targets(st.get(s) or {}) == [RA] for s in ALL_STEPS),
            {"approvals_T2": st.get("T2", {}).get("approval_callback_calls"), "T2": step_view(st.get("T2") or {}),
             "fact_records": fact_records(WA), "final_store_types": final_types(WA)})


def approval_view(case: dict[str, Any]) -> dict[str, Any]:
    st = steps_of(case)
    return {"approvals": {s: st.get(s, {}).get("approval_callback_calls") for s in TURN_STEPS},
            "T2": [st.get("T2", {}).get("run_state"), st.get("T2", {}).get("text")],
            "fact_records": len(fact_records(case)),
            "approval_session_events": {s: approval_events(st.get(s) or {}) for s in ALL_STEPS},
            "tool_adapter_memory_write": case.get("tool_adapter_memory_write")}


if baseline is not None:
    bcases = (baseline.get("observed") or {}).get("cases") or {}
    same, diff = {}, {}
    for label in ("W", "D", "WA", "N", "X", "P"):
        if label in cases and label in bcases:
            a, b = approval_view(cases[label]), approval_view(bcases[label])
            (same if a == b else diff)[label] = {"0.1.16": a, "control": b}
    c.check("item 2: ordinary model tool authorization/approval unchanged from the control run (the published "
            f"{baseline.get('info', {}).get('openminion.__version__')}): per case the approval callback calls, T2's "
            "run state and reply, the approved fact records and the turn tool adapter's memory.write decision are "
            "equal", bool(same) and not diff, {"equal": sorted(same), "different": diff,
                                               "views": {k: v["0.1.16"] for k, v in same.items()}})
    cmp = {label: [shape(cases[label]), shape(bcases[label])] for label in ("N", "X", "P")
           if label in cases and label in bcases}
    c.check("item 2: the allowed controls keep the records they had on the control run: decision titles per step and "
            "the T2 tool_outcome candidate are equal for N, X and P",
            bool(cmp) and all(a == b for a, b in cmp.values()),
            {k: {"0.1.16": v[0], "control": v[1]} for k, v in cmp.items()})

# item 3 ---------------------------------------------------------------------------------------------------
RS = ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8")
rst = steps_of(R)
EXPECT_R = {"R1": ALLOW, "R2": RA, "R3": RA, "R4": RA, "R5": ALLOW, "R6": ALLOW, "R7": ALLOW, "R8": ALLOW}
flags = {s: [r.get("memory_write_allowed_for_turn") for r in (rst.get(s) or {}).get("turn_runners", [])] for s in RS}
runner_ids = {s: [r.get("runner_id") for r in (rst.get(s) or {}).get("turn_runners", [])] for s in RS}
all_ids = {i for ids in runner_ids.values() for i in ids}
c.check("item 3: one runner reused across allow/deny/allow: R1 (N profile) SDK turn; profile rewritten to W and "
        "reloaded; R2 SDK turn, R3 consolidation run, R4 diagnostic turn; back to N; R5 SDK turn, R6 consolidation run, "
        "R7 diagnostic turn, R8 consolidation run: every one of the 8 turns runs on the same runner object, and the "
        "runner's per-turn memory.write answer is True, False, False, False, True, True, True, True",
        not R.get("case_error") and all(len(v) == 1 for v in runner_ids.values()) and len(all_ids) == 1
        and all_ids == {R.get("runner_id_at_start")}
        and [v[0] if v else None for v in flags.values()] == [True, False, False, False, True, True, True, True]
        and R.get("identity_constraints_after_switch_1") == [L.WRITE_CONSTRAINT]
        and R.get("identity_constraints_after_switch_2") == [L.NEUTRAL_CONSTRAINT],
        {"same_runner_object": len(all_ids) == 1 and all_ids == {R.get("runner_id_at_start")},
         "turns_per_step": {s: len(v) for s, v in runner_ids.items()},
         "memory_write_allowed_for_turn": flags,
         "turn_flags": {s: (step_view(rst.get(s) or {})["turn_runner_flags"]) for s in RS},
         "constraints_after_switches": [R.get("identity_constraints_after_switch_1"),
                                        R.get("identity_constraints_after_switch_2")],
         "case_error": R.get("case_error")})
c.check("item 3: each turn asks its current memory.write answer once (memory-capture), with no stale answer: allow, "
        "require_approval x3 (R2-R4, including the consolidation and diagnostic turns), then allow x4",
        all(capture_targets(rst.get(s) or {}) == [EXPECT_R[s]] for s in RS),
        {s: capture_targets(rst.get(s) or {}) for s in RS})
dec_counts = {s: len(new_decisions(rst.get(s) or {})) for s in RS}
dec_events = {s: dm_events(rst.get(s) or {}) for s in RS}
c.check("item 3: decision records follow each turn's answer: new decision records per turn 1, 0, 0, 0, 1, 1, 1, 1; "
        "the denied turns emit only brain.decision_memory.skipped (memory_write_not_authorized), the allowed turns "
        "brain.decision_memory.completed",
        list(dec_counts.values()) == [1, 0, 0, 0, 1, 1, 1, 1]
        and all(dec_events[s] and all(e == ["brain.decision_memory.skipped", "memory_write_not_authorized"]
                                      for e in dec_events[s]) for s in ("R2", "R3", "R4"))
        and all(["brain.decision_memory.completed", None] in dec_events[s]
                and not any(e[0] == "brain.decision_memory.skipped" for e in dec_events[s])
                for s in ("R1", "R5", "R6", "R7", "R8")),
        {"new_decision_records": dec_counts, "decision_memory_events": dec_events})
stage = {s: len(stage_calls(rst.get(s) or {})) for s in RS}
tos = {s: len(new_tool_outcomes(rst.get(s) or {})) for s in RS}
appr = {s: (rst.get(s) or {}).get("approval_callback_calls") for s in ("R1", "R2", "R5")}
c.check("item 3: tool_outcome staging follows each turn's answer: the refused memory.write call stages a candidate in "
        "R1 and R5 (allowed) and none in R2 (denied); the approval callback is asked once per SDK turn with the "
        "call's arguments; no other step stages one",
        [stage[s] for s in RS] == [1, 0, 0, 0, 1, 0, 0, 0]
        and all(v == [{"tool": "memory.write", "args": L.TOOL_ARGS}] for v in appr.values()),
        {"stage_candidate_calls": stage, "new_tool_outcome_rows": tos, "approval_callback_calls": appr})
cap = {s: [r.get("capture_authorized_for_turn") for r in (rst.get(s) or {}).get("turn_runners", [])] for s in RS}
extr = {s: (rst.get(s) or {}).get("provider_requests", []).count("extraction") for s in RS}
c.check("item 3: the consolidation (excluded) and diagnostic (capture disabled) turns get the answer itself but no "
        "capture: capture authorized per turn True, False, False, False, True, False, False, False; extraction requests "
        "per step: R1 1, R2 0, R4 0, R5 1, R7 0; the consolidation runs extract only in R6's recovery",
        [v[0] if v else None for v in cap.values()] == [True, False, False, False, True, False, False, False]
        and [extr[s] for s in ("R1", "R2", "R4", "R5", "R7")] == [1, 0, 0, 1, 0]
        and [extr[s] for s in ("R3", "R6", "R8")] == [0, 1, 0],
        {"capture_authorized_for_turn": cap, "extraction_requests": extr})

# item 4 ---------------------------------------------------------------------------------------------------
pairs = {"W.T1 vs N.T1": (steps_of(W).get("T1"), steps_of(N).get("T1")),
         "W.T2 vs N.T2": (steps_of(W).get("T2"), steps_of(N).get("T2")),
         "W.T3 vs N.T3": (steps_of(W).get("T3"), steps_of(N).get("T3")),
         "R.R2 vs R.R1": (rst.get("R2"), rst.get("R1"))}


def meta_view(step: dict[str, Any] | None) -> dict[str, Any]:
    step = step or {}
    calls = step.get("decision_memory_calls", [])
    ws = [w or {} for w in ws_after(step)]
    return {"routes": [x.get("decision_route") for x in calls],
            "reason_codes": [x.get("decision_reason_code") for x in calls],
            "state_reason_codes": [x.get("state_decision_reason_code") for x in calls],
            "persisted_reason_codes": [w.get("decision_reason_code") for w in ws],
            "persisted_refs": [w.get("decision_memory_refs") for w in ws],
            "persisted_rationale_present": [bool(w.get("decision_rationale")) for w in ws]}


meta = {k: {"denied": meta_view(a), "allowed": meta_view(b)} for k, (a, b) in pairs.items()}


def meta_ok(v: dict[str, Any]) -> bool:
    d, a = v["denied"], v["allowed"]
    return (bool(d["routes"]) and all(d["routes"]) and d["routes"] == a["routes"]
            and d["reason_codes"] == a["reason_codes"] and d["state_reason_codes"] == a["state_reason_codes"]
            and all(d["persisted_reason_codes"]) and d["persisted_reason_codes"] == a["persisted_reason_codes"]
            and all(r == [] for r in d["persisted_refs"]) and all(r for r in a["persisted_refs"]))


c.check("item 4: the working state keeps the decision metadata: in each denied turn write_decision_memory is still "
        "called with the turn's decision, and the decision's route and reason code, the working state's reason code "
        "and the persisted working state's decision fields equal those of the same step in the allowed run; only "
        "decision_memory_refs differs (empty when denied, the record id when allowed)",
        all(meta_ok(v) for v in meta.values()), meta)
w1 = steps_of(W).get("T1") or {}
r2 = rst.get("R2") or {}


def pending_view(step: dict[str, Any]) -> dict[str, Any]:
    result = step.get("memory_capture_bundle_result") or {}
    return {"memory_capture_state": step.get("memory_capture_state"),
            "error_code": result.get("error_code") if isinstance(result, dict) else result,
            "intent_capture_state": (step.get("intent") or {}).get("capture_state"),
            "extraction_requests": step.get("provider_requests", []).count("extraction")}


c.check("item 4: the unauthorized turn's capture stays pending: W's T1 and R's R2 end with capture state pending, "
        "error_code CAPTURE_RECOVERY_UNAUTHORIZED, no extraction request; R3's denied consolidation run keeps R2's "
        "intent pending (recovery 1 scanned, 0 recovered, 1 pending)",
        all(pending_view(s) == {"memory_capture_state": "pending", "error_code": "CAPTURE_RECOVERY_UNAUTHORIZED",
                                "intent_capture_state": "pending", "extraction_requests": 0} for s in (w1, r2))
        and (rst.get("R3") or {}).get("recovery") == [[1, 0, 1]],
        {"W.T1": pending_view(w1), "R.R2": pending_view(r2), "R3_recovery": (rst.get("R3") or {}).get("recovery"),
         "W.C_recovery": (steps_of(W).get("C") or {}).get("recovery")})
cr2 = R.get("capture_R2") or {}
c.check("item 4: later authorized recovery commits once: R6 (allowed) recovers R2's intent (1, 1, 0) with one extraction "
        "request; R8 finds nothing (0, 0, 0); at the end R2's capture has exactly one memory.capture.result event, one "
        "bundle receipt and its candidate rows, with no duplicate",
        (rst.get("R6") or {}).get("recovery") == [[1, 1, 0]] and (rst.get("R8") or {}).get("recovery") == [[0, 0, 0]]
        and cr2.get("memory_capture_result_events") == 1 and cr2.get("receipts") == 1
        and (cr2.get("rows_with_capture_id") or {}).get("memory_candidates") == 1,
        {"R6_recovery": (rst.get("R6") or {}).get("recovery"), "R8_recovery": (rst.get("R8") or {}).get("recovery"),
         "R6_extraction_requests": extr.get("R6"), "capture_R2": cr2, "capture_R1": R.get("capture_R1")})

observed["step_views"] = {k: {s: step_view(v) for s, v in steps_of(case).items()} for k, case in cases.items()}
summary = c.summary()
write_result(OUT, {"info": info, "summary": summary, "all_pass": c.all_pass, "checks": c.items, "observed": observed})
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
