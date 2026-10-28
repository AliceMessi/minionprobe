"""#449 PR check: recovery extraction on a store without atomic capture, then the same home reopened with the built-in store.

Installed package only (no repo test fixtures). The setup is the issue's
reproduction (probe_unsupported_extraction.py, same helpers, unchanged): a real
`APIRuntime` with one agent, `openminion`, whose `openai` provider points at a
scripted OpenAI-compatible server on 127.0.0.1; ONE unmodified SDK turn,
"Please remember that I prefer tabs over spaces for indentation.", in session
`capture`; the consolidation job created by the installed
`task.consolidate_memory` handler (package default batch_limit); the daemon's
own `attach_cron_scheduler`; runs queued one after another with
`TaskManager.run_task_now`. Nothing is injected.

  U  RUNS runs with the bundled `reference-sqlite` external backend (no
     `apply_capture_bundle`).
  R  the runtime is closed, and the same runtime home is reopened with the
     built-in memory store (no memory config registered); the same job runs
     REOPEN_RUNS more times.

Per run it records, with the call-through recorders of probe_420_lib.Observers:
the recovery result, which intents the extractor was called for, the provider
extraction requests and the bundle writes inside recovery, the pending intents
before and after, and the memory API's `supports_capture_bundles()` answer
("absent" where the installed package has no such method).

Whether a scheduled consolidation turn adds its own pending intent is #456: it
is recorded (`scheduled_turn_capture_states`), and the checks compare the
recovery counts with the pending intents observed before each run, so they hold
with and without #456's fix.

Usage: python -I -X utf8 probe_449_recovery.py <work_dir> <out.json> [runs] [reopen_runs]
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
RUNS = int(sys.argv[3]) if len(sys.argv) > 3 else 14
REOPEN_RUNS = int(sys.argv[4]) if len(sys.argv) > 4 else 3
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


info = banner("probe_449_recovery",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic")},
              openminion_direct_url=_direct_url(), runs=RUNS, reopen_runs=REOPEN_RUNS)

from openminion import Agent  # noqa: E402
from openminion.modules.memory.backends.external import register_reference_sqlite_backend  # noqa: E402
from openminion.tools.task.constants import DEFAULT_CONSOLIDATION_BATCH_LIMIT  # noqa: E402
from probe_420_lib import (  # noqa: E402
    AGENT_ID,
    CAPTURE_MESSAGE,
    Cron,
    Observers,
    ScriptedProvider,
    brain_events,
    build_runtime,
    chdir_env,
    restore_env,
    session_events,
)

c = Checks()
SESSION = "capture"
ROOT = WORK / "home"


def parse(value: Any) -> dict[str, Any]:
    try:
        return json.loads(value) if isinstance(value, str) else dict(value or {})
    except (TypeError, ValueError):
        return {"unparsed": value}


def create_job_with_defaults(cron: Cron) -> dict[str, Any]:
    """As in probe_unsupported_extraction.py: the handler's default batch_limit."""
    from openminion.modules.tool.runtime import RuntimeContext
    from openminion.modules.tool.runtime.policy import Policy
    from openminion.tools.task.plugin import _h_task_consolidate_memory

    workspace = cron.root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    policy = Policy(raw={"workspace_root": str(workspace), "context_metadata": {"agent_id": AGENT_ID},
                         "paths": {"read_allow": [str(workspace)], "write_allow": [str(workspace)], "deny": []},
                         "tools": {"allow_prefix": [""]}})
    ctx = RuntimeContext(policy=policy, workspace=workspace, run_root=cron.root / "tool-run", scope="WRITE_SAFE",
                         confirm=False, task_manager=cron.task_manager)
    return _h_task_consolidate_memory({"interval_hours": 12}, ctx)


def intents(runtime: Any) -> list[dict[str, Any]]:
    out = []
    for event in session_events(runtime.sessions, "turn.outcome"):
        p = event.payload
        if str(p.get("agent_id") or "") != AGENT_ID:
            continue
        cid = str(p.get("capture_id") or "")
        results = [e for e in session_events(runtime.sessions, "memory.capture.result")
                   if str(e.payload.get("capture_id") or "") == cid] if cid else []
        out.append({"capture_id": cid, "capture_state": p.get("capture_state"), "has_result": bool(results),
                    "results": len(results), "session": p.get("runtime_session_id"),
                    "root_turn_id": p.get("root_turn_id"), "created_at": event.created_at,
                    "payload": dict(p)})
    return sorted(out, key=lambda i: (str(i["created_at"]), str(i["session"]), i["capture_id"]))


def pending(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in items if i["capture_state"] == "pending" and not i["has_result"]]


def capability(obs: Observers) -> Any:
    predicate = getattr(obs.memctl, "supports_capture_bundles", None)
    if not callable(predicate):
        return "absent"
    try:
        return bool(predicate())
    except Exception as exc:  # noqa: BLE001 - recorded
        return f"{type(exc).__name__}: {exc}"


def run_phase(phase: str, *, external: str | None, runs: int, labels: dict[str, str],
              job_id: str | None) -> dict[str, Any]:
    provider = ScriptedProvider()
    old_env = chdir_env(ROOT)
    row: dict[str, Any] = {"phase": phase}
    runtime = obs = cron = None
    try:
        runtime = build_runtime(ROOT, provider, external_adapter=external)
        obs = Observers(runtime).install()
        store = obs.assembly.service._store
        backend = getattr(store, "_backend", None)
        inner = getattr(store, "_store", None)
        row["classes"] = {"memctl": type(obs.memctl).__name__, "memory_store": type(store).__name__,
                          "memory_backend": type(backend).__name__ if backend is not None else None,
                          "memory_store_inner": type(inner).__name__ if inner is not None else None}
        target = backend if backend is not None else inner
        row["store_has_callable_apply_capture_bundle"] = callable(getattr(target, "apply_capture_bundle", None))
        row["memctl_supports_capture_bundles"] = capability(obs)
        if job_id is None:
            provider_before = len(provider.requests)
            with Agent(runtime=runtime) as agent:
                res = agent.run(CAPTURE_MESSAGE, session_id=SESSION, timeout_seconds=120)
            md = dict(res.metadata or {})
            row["user_turn"] = {"run_state": res.run_state, "memory_capture_state": md.get("memory_capture_state"),
                                "memory_capture_bundle_result": parse(md.get("memory_capture_bundle_result")),
                                "provider_requests": [r["kind"] for r in provider.requests[provider_before:]]}
            for item in intents(runtime):
                labels.setdefault(item["capture_id"], "user")
        user = next(i for i in intents(runtime) if labels.get(i["capture_id"]) == "user")
        row["user_intent_at_start"] = {k: user[k] for k in ("capture_state", "has_result", "results")}
        row["user_intent_payload_at_start"] = user["payload"]
        cron = Cron(runtime, ROOT)
        if job_id is None:
            created = create_job_with_defaults(cron)
            job_id = str(created.get("task_id"))
            row["job_created"] = {k: created.get(k) for k in ("ok", "task_id", "batch_limit", "target_scope")}
        job = cron.store.get_cron_job(job_id) or {}
        row["job_id"] = job_id
        row["job_batch_limit"] = ((job.get("payload") or {}).get("_openminion_memory_consolidation") or {}).get(
            "batch_limit")
        cron.start()
        row["runs"] = []
        for index in range(1, runs + 1):
            before = pending(intents(runtime))
            marks = {k: len(getattr(obs, k)) for k in ("recoveries", "extractor_calls", "bundle_writes", "timeline")}
            provider_mark = len(provider.requests)
            queued = cron.run_now(job_id)
            done = cron.wait(job_id, str(queued.get("run_id")))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not any(
                    e.get("event") == "cron.run.finished" and e.get("run_id") == queued.get("run_id")
                    for e in obs.cron_events):
                time.sleep(0.05)
            after_all = intents(runtime)
            for item in after_all:
                labels.setdefault(item["capture_id"], f"scheduled turn of {phase} run {index}")
            recs = obs.recoveries[marks["recoveries"]:]
            windows = [(r["t_start"], r["t_end"]) for r in recs]
            in_recovery = [t for t in obs.timeline[marks["timeline"]:]
                           if t["what"] == "bundle_write" and any(s <= t["t"] <= e for s, e in windows)]
            rec_kinds = [k for s, e in windows for k in provider.kinds_between(s, e)]
            by_root = {i["root_turn_id"]: labels.get(i["capture_id"], "?") for i in after_all}
            user_now = next(i for i in after_all if labels.get(i["capture_id"]) == "user")
            run = {
                "run": index, "state": done.get("state"), "error": done.get("error"),
                "pending_before": len(before),
                "recovery": [r.get("result") for r in recs],
                "recovery_raised": [r.get("raised") for r in recs if r.get("raised")],
                "extractor_calls_for": [by_root.get(x.get("root_turn_id"), "?")
                                        for x in obs.extractor_calls[marks["extractor_calls"]:]],
                "recovery_provider_extractions": rec_kinds.count("extraction"),
                "recovery_bundle_writes": len(in_recovery),
                "pending_after": len(pending(after_all)),
                "scheduled_turn_intents": [{"capture_state": i["capture_state"], "has_result": i["has_result"]}
                                           for i in after_all
                                           if labels.get(i["capture_id"]) == f"scheduled turn of {phase} run {index}"],
                "user_intent": {k: user_now[k] for k in ("capture_state", "has_result", "results")},
                "user_intent_payload_unchanged": user_now["payload"] == user["payload"],
                "user_root_turn_extraction_started_events": sum(
                    1 for e in brain_events(obs.brain_session_api, SESSION, "brain.auto_fact_extraction",
                                            user.get("root_turn_id")) if e["event_type"].endswith(".started")),
            }
            row["runs"].append(run)
            print(f"{phase} run {index}: pending_before={run['pending_before']} recovery={run['recovery']} "
                  f"extractor_calls={len(run['extractor_calls_for'])} "
                  f"recovery_extractions={run['recovery_provider_extractions']} "
                  f"recovery_bundle_writes={run['recovery_bundle_writes']} pending_after={run['pending_after']} "
                  f"user_results={run['user_intent']['results']} state={run['state']}", flush=True)
        row["provider_requests_total"] = len(provider.requests)
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        import traceback

        row["case_error"] = f"{type(exc).__name__}: {exc}"
        row["case_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                    for f in traceback.extract_tb(exc.__traceback__)][-6:]
    finally:
        if cron is not None:
            try:
                cron.stop()
            except Exception as exc:  # noqa: BLE001
                row["scheduler_stop"] = f"{type(exc).__name__}: {exc}"
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


register_reference_sqlite_backend()
labels: dict[str, str] = {}
u = run_phase("U", external="reference-sqlite", runs=RUNS, labels=labels, job_id=None)
r = run_phase("R", external=None, runs=REOPEN_RUNS, labels=labels, job_id=u.get("job_id"))
ur, rr = u.get("runs", []), r.get("runs", [])
limit = u.get("job_batch_limit")

c.check("U. setup: reference-sqlite (SophiaGraphSqliteStore behind BackendMemoryStoreAdapter) has no callable "
        "apply_capture_bundle; the user turn completes, makes one extraction request, and its capture stays pending "
        "with CAPTURE_RECOVERY_UNSUPPORTED",
        u.get("classes", {}).get("memory_backend") == "SophiaGraphSqliteStore"
        and u.get("store_has_callable_apply_capture_bundle") is False
        and u.get("user_turn", {}).get("run_state") == "completed"
        and u["user_turn"].get("memory_capture_bundle_result", {}).get("error_code") == "CAPTURE_RECOVERY_UNSUPPORTED"
        and u["user_turn"].get("provider_requests", []).count("extraction") == 1
        and u.get("user_intent_at_start") == {"capture_state": "pending", "has_result": False, "results": 0},
        {"classes": u.get("classes"), "user_turn": u.get("user_turn")})
c.check("U. the memory API reports that the store cannot commit a capture bundle (supports_capture_bundles() is False)",
        u.get("memctl_supports_capture_bundles") is False, u.get("memctl_supports_capture_bundles"))
c.check(f"U. the job uses the package default batch_limit ({DEFAULT_CONSOLIDATION_BATCH_LIMIT}); all {RUNS} runs finish",
        limit == DEFAULT_CONSOLIDATION_BATCH_LIMIT and len(ur) == RUNS
        and all(x.get("state") == "finished" for x in ur),
        {"batch_limit": limit, "states": [x.get("state") for x in ur]})
c.check("U. counts unchanged: each run scans min(pending intents, batch_limit), recovers 0 and reports them all "
        "pending",
        all(x.get("recovery") == [[min(x["pending_before"], limit or 0), 0, min(x["pending_before"], limit or 0)]]
            for x in ur),
        [{k: x.get(k) for k in ("run", "pending_before", "recovery")} for x in ur])
c.check("U. no recovery extraction: in every run recovery calls no extractor, makes no provider extraction request "
        "and writes no bundle",
        all(x.get("extractor_calls_for") == [] and x.get("recovery_provider_extractions") == 0
            and x.get("recovery_bundle_writes") == 0 for x in ur),
        [{k: x.get(k) for k in ("run", "extractor_calls_for", "recovery_provider_extractions",
                                "recovery_bundle_writes")} for x in ur])
c.check("U. the user turn's intent stays pending, unchanged and without a result, and its root turn is not extracted "
        "again (its brain.auto_fact_extraction.started count does not grow)",
        all(x.get("user_intent") == {"capture_state": "pending", "has_result": False, "results": 0}
            and x.get("user_intent_payload_unchanged") is True for x in ur)
        and len({x.get("user_root_turn_extraction_started_events") for x in ur}) == 1,
        [{k: x.get(k) for k in ("run", "user_intent", "user_root_turn_extraction_started_events")} for x in ur])
# Recorded only: whether each scheduled turn adds its own pending intent is #456, not this fix.
scheduled_turn_states = [[i.get("capture_state") for i in x.get("scheduled_turn_intents", [])] for x in ur]
c.check("R. the same runtime home reopened with the built-in store (AuditedMemoryStore over SQLiteMemoryStore): the "
        "memory API reports that the store can commit a capture bundle",
        r.get("classes", {}).get("memory_store") == "AuditedMemoryStore"
        and r.get("classes", {}).get("memory_store_inner") == "SQLiteMemoryStore"
        and r.get("memctl_supports_capture_bundles") is True and r.get("job_id") == u.get("job_id"),
        {"classes": r.get("classes"), "supports": r.get("memctl_supports_capture_bundles"), "job": r.get("job_id")})
pending_at_reopen = rr[0].get("pending_before") if rr else None
c.check(f"R. the reopened runs recover every intent left pending by phase U once, batch_limit at a time: all "
        f"{REOPEN_RUNS} runs finish, each run recovers min(pending intents, batch_limit), nothing is left pending, the "
        "last run scans 0, and each recovered intent is extracted once (one extractor call, one provider extraction "
        "request and one bundle write per recovered intent)",
        len(rr) == REOPEN_RUNS and all(x.get("state") == "finished" for x in rr)
        and bool(ur) and pending_at_reopen == ur[-1].get("pending_after") and (pending_at_reopen or 0) >= 1
        and sum(x["recovery"][0][1] for x in rr if x.get("recovery")) == pending_at_reopen
        and all(x.get("recovery") == [[min(x["pending_before"], limit or 0), min(x["pending_before"], limit or 0), 0]]
                for x in rr)
        and rr[-1].get("recovery") == [[0, 0, 0]] and rr[-1].get("pending_after") == 0
        and all(len(x.get("extractor_calls_for", [])) == x["recovery"][0][1]
                == x.get("recovery_provider_extractions") == x.get("recovery_bundle_writes") for x in rr),
        [{k: x.get(k) for k in ("run", "pending_before", "recovery", "recovery_provider_extractions",
                                "recovery_bundle_writes", "pending_after")} for x in rr])
user_extractions = [f"{phase} run {x['run']}" for phase, runs in (("U", ur), ("R", rr)) for x in runs
                    for who in x.get("extractor_calls_for", []) if who == "user"]
c.check("U+R. the user turn's intent is recovered exactly once: one extractor call for it over all runs (in the first "
        "reopened run) and one memory.capture.result",
        user_extractions == ["R run 1"] and bool(rr)
        and all(x.get("user_intent", {}).get("results") == 1 for x in rr),
        {"user_extractions": user_extractions, "user_intent_after_R": [x.get("user_intent") for x in rr]})
hosts = sorted({h for x in (u, r) for h in x.get("provider_hosts", [])})
c.check("Offline provider: the openai base URL is the scripted server on 127.0.0.1 in both phases",
        hosts == ["127.0.0.1"], hosts)


def table(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"run": x["run"], "pending_before": x.get("pending_before"),
             "recovery": (x.get("recovery") or [None])[0],
             "recovery_extraction_requests": x.get("recovery_provider_extractions"),
             "user_intent_extracted": x.get("extractor_calls_for", []).count("user"),
             "scheduled_turn_capture_states": [i.get("capture_state") for i in x.get("scheduled_turn_intents", [])],
             "pending_after": x.get("pending_after")} for x in runs]


totals = {name: {"recovery_extraction_requests": sum(x.get("recovery_provider_extractions") or 0 for x in runs),
                 "recovered": sum((x.get("recovery") or [[0, 0, 0]])[0][1] for x in runs)}
          for name, runs in (("U", ur), ("R", rr))}
totals["user_intent_extractor_calls"] = len(user_extractions)
totals["U_pending_after_last_run"] = ur[-1].get("pending_after") if ur else None
write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "table": {"U": table(ur), "R": table(rr)}, "totals": totals,
                   "observed": {"U_reference_sqlite": u, "R_reopened_builtin": r}})
print(json.dumps({"totals": totals, "U_scheduled_turn_capture_states": scheduled_turn_states}, indent=None))
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
