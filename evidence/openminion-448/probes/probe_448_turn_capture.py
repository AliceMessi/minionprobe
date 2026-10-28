"""#448 PR probe: a turn's own capture asks the memory.write check that recovery asks.

Installed package only (no repo test fixtures); run with `python -I -X utf8`
from an empty directory outside any checkout. It reuses the issue probe's
helpers (`probe_420_lib.py`, `_common.py`, unchanged): a real `APIRuntime` for
one agent, `openminion`, whose `openai` provider is a scripted
OpenAI-compatible server on 127.0.0.1, an identity profile with one hard
constraint, and call-through recorders that never change a result. The probe
adds one recorder on the agent's `SecurityPolicyEngine.evaluate` that keeps
every `memory.write` decision.

  W  `NEVER write memory records`: one SDK turn with an approval callback that
     would approve; the recovery gate for the same capture; one memory
     consolidation run through the daemon's TaskManager and cron scheduler.
  R  the same runtime home reopened with `NEVER reveal secrets`: two more
     consolidation runs of the same job.
  C  `NEVER reveal secrets` (control): one SDK turn.

Usage: python -I -X utf8 probe_448_turn_capture.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import sys
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


info = banner("probe_448_turn_capture",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic")},
              openminion_direct_url=_direct_url())

from openminion import Agent  # noqa: E402
from openminion.modules.policy.adapters.memory import memory_capture_recovery_allowed  # noqa: E402
from openminion.modules.session.capture import build_capture_identity  # noqa: E402
from openminion.services.agent.execution.composition import AgentServiceTurnFlowAdapter  # noqa: E402
from probe_420_lib import (  # noqa: E402
    CAPTURE_ITEM,
    CAPTURE_MESSAGE,
    Cron,
    Observers,
    ScriptedProvider,
    build_runtime,
    chdir_env,
    intent_view,
    restore_env,
    session_events,
    sqlite_rows,
)

WRITE_CONSTRAINT = "NEVER write memory records"
NEUTRAL_CONSTRAINT = "NEVER reveal secrets"
SESSION = "capture"
UNAUTHORIZED = "CAPTURE_RECOVERY_UNAUTHORIZED"
c = Checks()


def parse(value: Any) -> dict[str, Any]:
    try:
        return json.loads(value) if isinstance(value, str) else dict(value or {})
    except (TypeError, ValueError):
        return {"unparsed": value}


def record_memory_write_decisions(obs: Any) -> tuple[list[dict[str, Any]], Any]:
    decisions: list[dict[str, Any]] = []
    policy = obs.security_policy
    inner = policy.evaluate  # Observers' call-through recorder, itself wrapping the original

    def evaluate(check: Any, *args: Any, **kwargs: Any) -> Any:
        decision = inner(check, *args, **kwargs)
        if check.action.tool_name == "memory.write":
            ctx = check.context
            decisions.append({"channel": ctx.channel, "target": ctx.target, "session_id": ctx.session_id,
                              "run_id": ctx.run_id, "actor_scopes": sorted(check.actor.scopes),
                              "decision": decision.decision, "reason_code": decision.reason_code,
                              "t": time.monotonic()})
        return decision

    policy.evaluate = evaluate
    return decisions, (policy, inner)


def store_rows(obs: Any, capture_id: str) -> dict[str, Any]:
    store = obs.assembly.service._store
    inner = getattr(store, "_store", None)
    db = Path(str(inner.db_path)) if inner is not None and getattr(inner, "db_path", None) else None
    if db is None:
        return {"store": "not a built-in SQLite memory store"}
    return {
        "capture_candidates": sqlite_rows(db, "SELECT candidate_id, type, key, status FROM memory_candidates "
                                              "WHERE json_extract(meta_json, '$.capture_id') = ?", (capture_id,)),
        "receipts": sqlite_rows(db, "SELECT capture_id, disposition FROM memory_capture_bundle_receipts "
                                    "WHERE capture_id = ?", (capture_id,)),
        "record_types": sqlite_rows(db, "SELECT type, COUNT(*) FROM memory_records GROUP BY type ORDER BY type"),
        "candidate_types": sqlite_rows(db, "SELECT type, COUNT(*) FROM memory_candidates GROUP BY type ORDER BY type"),
    }


def turn(root: Path, constraint: str) -> dict[str, Any]:
    """One SDK turn; for W also the recovery gate and one consolidation run."""
    provider = ScriptedProvider()
    old_env = chdir_env(root)
    row: dict[str, Any] = {"constraint": constraint}
    runtime = obs = cron = undo = None
    try:
        runtime = build_runtime(root, provider, identity_constraint=constraint)
        obs = Observers(runtime).install()
        row["identity_constraints"] = list(getattr(obs.security_policy, "_identity_constraints", []) or [])
        decisions, undo = record_memory_write_decisions(obs)
        approvals: list[str] = []
        t0 = time.monotonic()
        mark = len(provider.requests)
        with Agent(runtime=runtime) as agent:
            res = agent.run(CAPTURE_MESSAGE, session_id=SESSION, timeout_seconds=120,
                            approval_callback=lambda name, *_a: approvals.append(str(name)) or True)
        t1 = time.monotonic()
        md = dict(res.metadata or {})
        [intent] = [intent_view(e) for e in session_events(runtime.sessions, "turn.outcome", SESSION)]
        capture_id = str(intent.get("capture_id") or "")
        row["turn"] = {"run_state": res.run_state, "text": res.text,
                       "memory_capture_state": md.get("memory_capture_state"),
                       "memory_capture_bundle_result": parse(md.get("memory_capture_bundle_result")),
                       "provider_requests": [r["kind"] for r in provider.requests[mark:]],
                       "bundle_writes": [{k: w.get(k) for k in ("capture_id", "candidates", "raised")}
                                         for w in obs.bundle_writes],
                       "memory_write_decisions": [{k: v for k, v in d.items() if k != "t"} for d in decisions
                                                  if t0 <= d["t"] <= t1],
                       "approval_callback_calls": list(approvals)}
        row["intent"] = intent
        row["results"] = [dict(e.payload) for e in session_events(runtime.sessions, "memory.capture.result")
                          if e.payload.get("capture_id") == capture_id]
        row["store_after_turn"] = store_rows(obs, capture_id)
        if constraint == WRITE_CONSTRAINT:
            identity = build_capture_identity(runtime_session_id=SESSION, root_turn_id=intent["root_turn_id"])
            port = AgentServiceTurnFlowAdapter(obs.service)
            mark_d = len(decisions)
            allowed = memory_capture_recovery_allowed(
                SESSION, identity.root_turn_id, identity.event_id, identity.capture_id,
                policy=port.security_policy, tools=port.tools, agent_id=port.identity_agent_id)
            row["recovery_gate"] = {"returned": allowed,
                                    "decisions": [{k: v for k, v in d.items() if k != "t"}
                                                  for d in decisions[mark_d:]]}
            cron = Cron(runtime, root)
            created = cron.create_consolidation_job(batch_limit=8)
            row["job_id"] = str(created.get("task_id"))
            cron.start()
            row["runs"] = [consolidation_run(cron, obs, provider, decisions, row["job_id"], capture_id)]
            row["store_after_runs"] = store_rows(obs, capture_id)
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        import traceback

        row["case_error"] = f"{type(exc).__name__}: {exc}"
        row["case_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                    for f in traceback.extract_tb(exc.__traceback__)][-6:]
    finally:
        close(row, cron, obs, undo, runtime, old_env, provider)
    return row


def consolidation_run(cron: Any, obs: Any, provider: Any, decisions: list[dict[str, Any]], job_id: str,
                      capture_id: str) -> dict[str, Any]:
    marks = {k: len(getattr(obs, k)) for k in ("recoveries", "extractor_calls", "bundle_writes")}
    mark_p, mark_d = len(provider.requests), len(decisions)
    queued = cron.run_now(job_id)
    done = cron.wait(job_id, str(queued.get("run_id")))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not any(
            e.get("event") == "cron.run.finished" and e.get("run_id") == queued.get("run_id")
            for e in obs.cron_events):
        time.sleep(0.05)
    recs = obs.recoveries[marks["recoveries"]:]
    windows = [(r["t_start"], r["t_end"]) for r in recs]
    return {
        "state": done.get("state"), "error": done.get("error"),
        "recovery": [r.get("result") for r in recs],
        "extractor_calls": [x.get("root_turn_id") for x in obs.extractor_calls[marks["extractor_calls"]:]],
        "recovery_provider_extractions": [k for s, e in windows for k in provider.kinds_between(s, e)].count(
            "extraction"),
        "recovery_bundle_writes": [w.get("capture_id") for w in obs.bundle_writes[marks["bundle_writes"]:]
                                   if w.get("capture_id") == capture_id],
        "recovery_decisions": [{k: v for k, v in d.items() if k not in ("t", "actor_scopes")}
                               for d in decisions[mark_d:] if d["target"] == "memory-capture-recovery"],
        "provider_requests": len(provider.requests) - mark_p,
        "results_for_capture": len([e for e in session_events(obs.runtime.sessions, "memory.capture.result")
                                    if e.payload.get("capture_id") == capture_id]),
    }


def reopen(root: Path, job_id: str, capture_id: str) -> dict[str, Any]:
    """R: the W home with the neutral profile; two consolidation runs of the same job."""
    provider = ScriptedProvider()
    old_env = chdir_env(root)
    row: dict[str, Any] = {"constraint": NEUTRAL_CONSTRAINT}
    runtime = obs = cron = undo = None
    try:
        runtime = build_runtime(root, provider, identity_constraint=NEUTRAL_CONSTRAINT)
        obs = Observers(runtime).install()
        row["identity_constraints"] = list(getattr(obs.security_policy, "_identity_constraints", []) or [])
        decisions, undo = record_memory_write_decisions(obs)
        cron = Cron(runtime, root)
        cron.start()
        row["runs"] = [consolidation_run(cron, obs, provider, decisions, job_id, capture_id) for _ in range(2)]
        row["results"] = [dict(e.payload) for e in session_events(runtime.sessions, "memory.capture.result")
                          if e.payload.get("capture_id") == capture_id]
        row["store_after_runs"] = store_rows(obs, capture_id)
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        import traceback

        row["case_error"] = f"{type(exc).__name__}: {exc}"
        row["case_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                    for f in traceback.extract_tb(exc.__traceback__)][-6:]
    finally:
        close(row, cron, obs, undo, runtime, old_env, provider)
    return row


def close(row: dict[str, Any], cron: Any, obs: Any, undo: Any, runtime: Any, old_env: Any, provider: Any) -> None:
    if cron is not None:
        try:
            cron.stop()
        except Exception as exc:  # noqa: BLE001
            row["scheduler_stop"] = f"{type(exc).__name__}: {exc}"
    if undo is not None:
        undo[0].evaluate = undo[1]
    if obs is not None:
        obs.uninstall()
    if runtime is not None:
        try:
            runtime.close()
        except Exception as exc:  # noqa: BLE001
            row["runtime_close"] = f"{type(exc).__name__}: {exc}"
    restore_env(old_env)
    provider.close()


w = turn(WORK / "W", WRITE_CONSTRAINT)
# build_runtime rewrites W's profile.yaml with the neutral constraint.
r = reopen(WORK / "W", str(w.get("job_id")), str(w.get("intent", {}).get("capture_id")))
k = turn(WORK / "C", NEUTRAL_CONSTRAINT)
observed = {"W_write_constraint": w, "R_reopened_neutral": r, "C_neutral_constraint": k}

wt, kt = w.get("turn", {}), k.get("turn", {})
wi, ki = w.get("intent", {}), k.get("intent", {})
w_capture = wi.get("capture_id")


def turn_decision(intent: dict[str, Any], decision: str, reason: str) -> list[dict[str, Any]]:
    return [{"channel": "console", "target": "memory-capture", "session_id": SESSION,
             "run_id": intent.get("root_turn_id"), "decision": decision, "reason_code": reason}]


def strip_scopes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k2: v for k2, v in d.items() if k2 != "actor_scopes"} for d in rows]


c.check("W/R/C. the identity profile is loaded in each runtime (the agent's SecurityPolicyEngine holds the constraint)",
        w.get("identity_constraints") == [WRITE_CONSTRAINT] and r.get("identity_constraints") == [NEUTRAL_CONSTRAINT]
        and k.get("identity_constraints") == [NEUTRAL_CONSTRAINT],
        {"W": w.get("identity_constraints"), "R": r.get("identity_constraints"), "C": k.get("identity_constraints")})
c.check("W. the SDK turn completes with the same reply as the control turn C",
        wt.get("run_state") == "completed" and kt.get("run_state") == "completed"
        and wt.get("text") == kt.get("text") and bool(wt.get("text")),
        {"W": [wt.get("run_state"), wt.get("text")], "C": [kt.get("run_state"), kt.get("text")]})
c.check("W. the turn asks memory.write once, before its capture, with the turn's context (channel console, target "
        "memory-capture, runtime session `capture`, root turn) and recovery's actor: require_approval, "
        "identity_hard_constraint_restricted",
        strip_scopes(wt.get("memory_write_decisions", []))
        == turn_decision(wi, "require_approval", "identity_hard_constraint_restricted"),
        wt.get("memory_write_decisions"))
c.check("W. no extraction request, no memctl bundle write, no capture candidate, no receipt and no "
        "memory.capture.result for the turn's capture",
        "extraction" not in wt.get("provider_requests", ["?"]) and wt.get("bundle_writes") == []
        and w.get("store_after_turn", {}).get("capture_candidates") == []
        and w.get("store_after_turn", {}).get("receipts") == [] and w.get("results") == [],
        {"provider": wt.get("provider_requests"), "bundle_writes": wt.get("bundle_writes"),
         "store": w.get("store_after_turn"), "results": w.get("results")})
c.check("W. the intent stays pending; memory_capture_state pending and memory_capture_bundle_result "
        f"{{capture_id, disposition pending, error_code {UNAUTHORIZED}}}",
        wi.get("capture_state") == "pending" and wt.get("memory_capture_state") == "pending"
        and wt.get("memory_capture_bundle_result") == {"capture_id": w_capture, "disposition": "pending",
                                                       "error_code": UNAUTHORIZED},
        {"intent": wi, "state": wt.get("memory_capture_state"), "bundle": wt.get("memory_capture_bundle_result")})
c.check("W. no approval callback call for the capture (the SDK callback would approve)",
        wt.get("approval_callback_calls") == [], wt.get("approval_callback_calls"))
rg = w.get("recovery_gate", {})
td = (wt.get("memory_write_decisions") or [{}])[0]
c.check("W. the recovery gate for the same capture returns False with the same decision, actor scopes, session and "
        "root turn; it differs only by channel cron and target memory-capture-recovery",
        rg.get("returned") is False and len(rg.get("decisions", [])) == 1
        and {k2: v for k2, v in rg["decisions"][0].items() if k2 not in ("channel", "target")}
        == {k2: v for k2, v in td.items() if k2 not in ("channel", "target", "t")}
        and (rg["decisions"][0].get("channel"), rg["decisions"][0].get("target")) == ("cron",
                                                                                     "memory-capture-recovery"),
        rg)
wr = (w.get("runs") or [{}])[0]
c.check("W. a memory consolidation run (daemon TaskManager + cron scheduler) is denied before extraction: recovery "
        "(1, 0, 1), no extractor call, no recovery extraction request or bundle write, still no result",
        wr.get("state") == "finished" and wr.get("recovery") == [[1, 0, 1]] and wr.get("extractor_calls") == []
        and wr.get("recovery_provider_extractions") == 0 and wr.get("recovery_bundle_writes") == []
        and wr.get("results_for_capture") == 0
        and [d.get("decision") for d in wr.get("recovery_decisions", [])] == ["require_approval"],
        wr)
r1, r2 = (list(r.get("runs") or []) + [{}, {}])[:2]
c.check("R. the same home reopened with the neutral profile: the next run recovers the turn's intent once "
        "(recovery (1, 1, 0), one extractor call for its root turn, one bundle write), the run after it scans "
        "nothing, and the capture has exactly one memory.capture.result (succeeded, one output)",
        r1.get("recovery") == [[1, 1, 0]] and r1.get("extractor_calls") == [wi.get("root_turn_id")]
        and r1.get("recovery_bundle_writes") == [w_capture] and r2.get("recovery") == [[0, 0, 0]]
        and r2.get("extractor_calls") == [] and len(r.get("results", [])) == 1
        and r["results"][0].get("disposition") == "succeeded" and len(r["results"][0].get("output_ids", [])) == 1,
        {"run1": r1, "run2": r2, "results": r.get("results")})
kw = kt.get("bundle_writes", [])
c.check("C. control: the same turn with the neutral constraint captures as before: one extraction request, one bundle "
        "write with the candidate, memory_capture_state succeeded, one candidate, one receipt and one result",
        kt.get("provider_requests", []).count("extraction") == 1 and len(kw) == 1
        and [i.get("normalized_key") for i in kw[0].get("candidates", [])] == [CAPTURE_ITEM["normalized_key"]]
        and kt.get("memory_capture_state") == "succeeded"
        and len(k.get("store_after_turn", {}).get("capture_candidates", [])) == 1
        and len(k.get("store_after_turn", {}).get("receipts", [])) == 1 and len(k.get("results", [])) == 1,
        {"provider": kt.get("provider_requests"), "bundle_writes": kw, "state": kt.get("memory_capture_state"),
         "store": k.get("store_after_turn"), "results": len(k.get("results", []))})
c.check("C. control: the turn asks memory.write once with the turn's context and is allowed",
        strip_scopes(kt.get("memory_write_decisions", [])) == turn_decision(ki, "allow", "allowed"),
        kt.get("memory_write_decisions"))
hosts = sorted({h for row in (w, r, k) for h in row.get("provider_hosts", [])})
c.check("Offline provider: every case's openai base URL is the scripted server on 127.0.0.1", hosts == ["127.0.0.1"],
        hosts)

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": observed})
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
