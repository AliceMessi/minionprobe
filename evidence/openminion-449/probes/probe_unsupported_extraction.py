"""Repeated capture extraction and pending growth on a backend without atomic capture (#397 contract), bounded runs.

Installed package only (no repo test fixtures). Each case builds a real
`APIRuntime` (one agent, `openminion`; the SDK's `openai` provider pointed at a
scripted OpenAI-compatible server on 127.0.0.1, no external network), runs ONE
unmodified SDK turn, "Please remember that I prefer tabs over spaces for
indentation.", in session `capture`, creates the memory consolidation job with
the installed `task.consolidate_memory` handler (package default batch_limit,
no argument) through the daemon's TaskManager, starts the daemon's own
`attach_cron_scheduler(runtime=...)` and queues RUNS runs one after another
with `TaskManager.run_task_now` (the `task.run_now` path). Nothing is injected.

  U  the bundled `reference-sqlite` external backend (SophiaGraphSqliteStore
     behind BackendMemoryStoreAdapter; no `apply_capture_bundle`)
  S  control: the built-in memory store (AuditedMemoryStore over SQLiteMemoryStore)

Per run the probe records, with call-through wrappers that only record and then
call the original (probe_420_lib.Observers, plus recorders on submit_turn's
request meta and on the brain's `capture_is_excluded` call):
  - pending intents before and after the run (turn.outcome, capture_state
    pending, agent openminion, no memory.capture.result), labelled by origin
    (the user turn, or the scheduled turn of run j);
  - the recovery result, which intents the extractor was called for, the
    provider extraction requests and the bundle writes inside recovery;
  - the scheduled turn: its own intent, its capture result and its own
    extraction request;
  - the brain.auto_fact_extraction events for the user turn's root turn.

Usage: python -I -X utf8 probe_unsupported_extraction.py <work_dir> <out.json> [runs]
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


info = banner("probe_unsupported_extraction",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic")},
              openminion_direct_url=_direct_url(), runs=RUNS)

import openminion.services.brain.post_execution.mixin as brain_mixin  # noqa: E402
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
observed: dict[str, Any] = {}
SESSION = "capture"


def parse(value: Any) -> dict[str, Any]:
    try:
        return json.loads(value) if isinstance(value, str) else dict(value or {})
    except (TypeError, ValueError):
        return {"unparsed": value}


def create_job_with_defaults(cron: Cron) -> dict[str, Any]:
    """Cron.create_consolidation_job, but without a batch_limit argument (the handler's default)."""
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
        has_result = bool(cid) and runtime.sessions.get_event_by_canonical_id(f"memory.capture.result:{cid}") is not None
        out.append({"capture_id": cid, "capture_state": p.get("capture_state"), "has_result": has_result,
                    "session": p.get("runtime_session_id"), "root_turn_id": p.get("root_turn_id"),
                    "created_at": event.created_at})
    return sorted(out, key=lambda i: (str(i["created_at"]), str(i["session"]), i["capture_id"]))


def pending(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in items if i["capture_state"] == "pending" and not i["has_result"]]


def run_case(name: str, *, external: str | None) -> dict[str, Any]:
    root = WORK / name
    provider = ScriptedProvider()
    old_env = chdir_env(root)
    row: dict[str, Any] = {}
    runtime = obs = cron = None
    undo: list[tuple[Any, str, Any]] = []
    try:
        runtime = build_runtime(root, provider, external_adapter=external)
        obs = Observers(runtime).install()
        store = obs.assembly.service._store
        backend = getattr(store, "_backend", None)
        inner = getattr(store, "_store", None)
        row["classes"] = {"memory_store": type(store).__name__,
                          "memory_backend": type(backend).__name__ if backend is not None else None,
                          "memory_store_inner": type(inner).__name__ if inner is not None else None}
        # The capability BackendMemoryStoreAdapter.apply_capture_bundle checks, read before any turn or run.
        target = backend if backend is not None else inner
        row["capture_capability_before_runs"] = {
            "object": type(target).__name__,
            "has_callable_apply_capture_bundle": callable(getattr(target, "apply_capture_bundle", None))}
        # -------------------------------------------------- extra call-through recorders
        submit_meta: list[dict[str, Any]] = []
        inner_submit = runtime.runtime_manager.submit_turn

        def submit(request: Any, *args: Any, **kwargs: Any) -> Any:
            meta = dict(getattr(request, "meta", {}) or {})
            submit_meta.append({"session_id": getattr(request, "session_id", None),
                                "memory_consolidation_job": meta.get("memory_consolidation_job"),
                                "cron_run_id_present": bool(meta.get("cron_run_id"))})
            return inner_submit(request, *args, **kwargs)

        runtime.runtime_manager.submit_turn = submit
        undo.append((runtime.runtime_manager, "submit_turn", None))
        exclusion_calls: list[dict[str, Any]] = []
        original_excluded = brain_mixin.capture_is_excluded

        def capture_is_excluded(md: Any) -> bool:
            result = original_excluded(md)
            data = dict(md or {})
            exclusion_calls.append({"memory_consolidation_job": data.get("memory_consolidation_job"),
                                    "cron_run_id_present": bool(data.get("cron_run_id")), "returned": result})
            return result

        brain_mixin.capture_is_excluded = capture_is_excluded
        undo.append((brain_mixin, "capture_is_excluded", original_excluded))
        # -------------------------------------------------- the user turn (unmodified)
        provider_before = len(provider.requests)
        with Agent(runtime=runtime) as agent:
            res = agent.run(CAPTURE_MESSAGE, session_id=SESSION, timeout_seconds=120)
        md = dict(res.metadata or {})
        row["user_turn"] = {"run_state": res.run_state, "memory_capture_state": md.get("memory_capture_state"),
                            "memory_capture_bundle_result": parse(md.get("memory_capture_bundle_result")),
                            "provider_requests": [r["kind"] for r in provider.requests[provider_before:]],
                            "bundle_writes": [{"candidates": len(w.get("candidates", [])),
                                               "raised": w.get("raised"), "injected": w.get("injected"),
                                               "disposition": (w.get("result") or {}).get("disposition")
                                               if isinstance(w.get("result"), dict) else None}
                                              for w in obs.bundle_writes]}
        all_intents = intents(runtime)
        labels: dict[str, str] = {i["capture_id"]: "user" for i in all_intents}
        user = all_intents[0] if all_intents else {}
        row["user_intent"] = user
        # -------------------------------------------------- the job and RUNS runs
        cron = Cron(runtime, root)
        created = create_job_with_defaults(cron)
        job_id = str(created.get("task_id"))
        job = cron.store.get_cron_job(job_id) or {}
        row["job"] = {"created": {k: created.get(k) for k in ("ok", "task_id", "batch_limit", "target_scope")},
                      "payload_consolidation": (job.get("payload") or {}).get("_openminion_memory_consolidation"),
                      "package_default_batch_limit": DEFAULT_CONSOLIDATION_BATCH_LIMIT}
        cron.start()
        row["runs"] = []
        for index in range(1, RUNS + 1):
            before = pending(intents(runtime))
            marks = {k: len(getattr(obs, k)) for k in ("recoveries", "extractor_calls", "bundle_writes", "timeline")}
            submit_mark, excl_mark = len(submit_meta), len(exclusion_calls)
            provider_mark = len(provider.requests)
            run: dict[str, Any] = {"run": index}
            queued = cron.run_now(job_id)
            done = cron.wait(job_id, str(queued.get("run_id")))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not any(
                    e.get("event") == "cron.run.finished" and e.get("run_id") == queued.get("run_id")
                    for e in obs.cron_events):
                time.sleep(0.05)
            after_all = intents(runtime)
            for item in after_all:
                labels.setdefault(item["capture_id"], f"scheduled turn of run {index}")
            after = pending(after_all)
            recs = obs.recoveries[marks["recoveries"]:]
            windows = [(r["t_start"], r["t_end"]) for r in recs]
            in_recovery = [t for t in obs.timeline[marks["timeline"]:]
                           if t["what"] == "bundle_write" and any(s <= t["t"] <= e for s, e in windows)]
            recovery_capture_ids = {t.get("capture_id") for t in in_recovery}
            writes = obs.bundle_writes[marks["bundle_writes"]:]
            rec_writes = [w for w in writes if w.get("capture_id") in recovery_capture_ids
                          and str(w.get("thread", "")).startswith("openminion-cron-run")]
            turn_writes = [w for w in writes if w not in rec_writes]
            kinds = [r["kind"] for r in provider.requests[provider_mark:]]
            rec_kinds = [k for s, e in windows for k in provider.kinds_between(s, e)]
            new_intents = [i for i in after_all if labels.get(i["capture_id"]) == f"scheduled turn of run {index}"]
            run.update({
                "state": done.get("state"), "error": done.get("error"),
                "pending_before": len(before), "pending_before_labels": [labels.get(i["capture_id"]) for i in before],
                "recovery": [r.get("result") for r in recs],
                "recovery_limit": [r.get("limit") for r in recs],
                "extractor_calls_for": [labels.get(next((i["capture_id"] for i in after_all
                                                         if i["root_turn_id"] == x.get("root_turn_id")), ""), "?")
                                        for x in obs.extractor_calls[marks["extractor_calls"]:]],
                "recovery_provider_extractions": rec_kinds.count("extraction"),
                "recovery_bundle_writes": len(rec_writes),
                "recovery_bundle_writes_unsupported": sum("CaptureRecoveryUnsupportedError" in str(w.get("raised"))
                                                          for w in rec_writes),
                "recovery_bundle_writes_raised_other": [w.get("raised") for w in rec_writes
                                                        if w.get("raised") and "CaptureRecoveryUnsupportedError"
                                                        not in str(w.get("raised"))],
                "scheduled_turn_intents": [{"capture_state": i["capture_state"], "has_result": i["has_result"]}
                                           for i in new_intents],
                "scheduled_turn_bundle_writes": [{"candidates": len(w.get("candidates", [])),
                                                  "raised": w.get("raised"),
                                                  "disposition": (w.get("result") or {}).get("disposition")
                                                  if isinstance(w.get("result"), dict) else None}
                                                 for w in turn_writes],
                "scheduled_turn_provider_extractions": kinds.count("extraction") - rec_kinds.count("extraction"),
                "provider_requests_by_kind": {k: kinds.count(k) for k in sorted(set(kinds))},
                "submit_meta": submit_meta[submit_mark:],
                "brain_capture_is_excluded_calls": exclusion_calls[excl_mark:],
                "pending_after": len(after),
                "results_total": len(session_events(runtime.sessions, "memory.capture.result")),
                "user_root_turn_extraction_started_events": sum(
                    1 for e in brain_events(obs.brain_session_api, SESSION, "brain.auto_fact_extraction",
                                            user.get("root_turn_id")) if e["event_type"].endswith(".started")),
            })
            row["runs"].append(run)
            print(f"{name} run {index}: pending_before={run['pending_before']} recovery={run['recovery']} "
                  f"recovery_extractions={run['recovery_provider_extractions']} "
                  f"scheduled_turn_extractions={run['scheduled_turn_provider_extractions']} "
                  f"pending_after={run['pending_after']} state={run['state']}", flush=True)
        row["labels_in_order"] = [labels.get(i["capture_id"]) for i in intents(runtime)]
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
        for obj, name_, original in reversed(undo):
            if original is None:
                try:
                    delattr(obj, name_)
                except AttributeError:
                    pass
            else:
                setattr(obj, name_, original)
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
u = run_case("U", external="reference-sqlite")
observed["U_reference_sqlite"] = u
s = run_case("S", external=None)
observed["S_builtin_control"] = s

limit = (u.get("job", {}).get("created") or {}).get("batch_limit")
ur = u.get("runs", [])
sr = s.get("runs", [])
c.check("U. setup: the bundled reference-sqlite backend (SophiaGraphSqliteStore behind BackendMemoryStoreAdapter) has "
        "no callable apply_capture_bundle before any run; the unmodified user turn completes, makes one extraction "
        "request, and its capture stays pending with CAPTURE_RECOVERY_UNSUPPORTED and no result",
        u.get("classes", {}).get("memory_backend") == "SophiaGraphSqliteStore"
        and u.get("capture_capability_before_runs", {}).get("has_callable_apply_capture_bundle") is False
        and u.get("user_turn", {}).get("run_state") == "completed"
        and u["user_turn"].get("memory_capture_bundle_result", {}).get("error_code") == "CAPTURE_RECOVERY_UNSUPPORTED"
        and u["user_turn"].get("provider_requests", []).count("extraction") == 1
        and u.get("user_intent", {}).get("capture_state") == "pending" and not u["user_intent"].get("has_result"),
        {"classes": u.get("classes"), "capability": u.get("capture_capability_before_runs"),
         "user_turn": u.get("user_turn")})
c.check(f"U. the job uses the package default batch_limit ({DEFAULT_CONSOLIDATION_BATCH_LIMIT}), which is the "
        f"recovery limit in every run; all {RUNS} runs finish",
        limit == DEFAULT_CONSOLIDATION_BATCH_LIMIT and len(ur) == RUNS
        and all(r.get("recovery_limit") == [limit] and r.get("state") == "finished" for r in ur),
        {"job": u.get("job"), "states": [r.get("state") for r in ur]})
c.check("U. pending growth: before run k there are k pending intents (the user turn's plus the k-1 earlier scheduled "
        f"turns'), and after {RUNS} runs there are {RUNS + 1}; no memory.capture.result is ever written",
        all(r.get("pending_before") == r["run"] and r.get("pending_after") == r["run"] + 1 for r in ur)
        and all(r.get("results_total") == 0 for r in ur),
        [{k: r.get(k) for k in ("run", "pending_before", "pending_after", "results_total")} for r in ur])
c.check("U. repeated extraction: run k scans min(k, batch_limit) intents, calls the extractor once per scanned intent and "
        "makes that many provider extraction requests inside recovery; every recovery bundle write raises "
        "CaptureRecoveryUnsupportedError (no other error); recovered 0",
        all(r.get("recovery") == [[min(r["run"], limit or 0), 0, min(r["run"], limit or 0)]]
            and len(r.get("extractor_calls_for", [])) == min(r["run"], limit or 0)
            and r.get("recovery_provider_extractions") == min(r["run"], limit or 0)
            and r.get("recovery_bundle_writes") == min(r["run"], limit or 0)
            and r.get("recovery_bundle_writes_unsupported") == min(r["run"], limit or 0)
            and r.get("recovery_bundle_writes_raised_other") == [] for r in ur),
        [{k: r.get(k) for k in ("run", "recovery", "recovery_provider_extractions", "recovery_bundle_writes",
                                "recovery_bundle_writes_unsupported")} for r in ur])
c.check("U. the user turn's root turn is extracted again in every run: the brain.auto_fact_extraction.started events "
        "for it in the brain session API grow by one per run (1 after run 1, 2 after run 2, ...)",
        [r.get("user_root_turn_extraction_started_events") for r in ur] == list(range(1, RUNS + 1)),
        [r.get("user_root_turn_extraction_started_events") for r in ur])
late = [r for r in ur if r["run"] > (limit or 0)]
oldest = ["user"] + [f"scheduled turn of run {j}" for j in range(1, (limit or 0))]
c.check("U. at the bound (runs after batch_limit) recovery scans the same oldest batch_limit intents every run (the user "
        "turn's and the first batch_limit-1 scheduled turns'); the newer pending intents are not scanned",
        bool(late) and all(sorted(r.get("extractor_calls_for", [])) == sorted(oldest) for r in late),
        {r["run"]: {"scanned": r.get("extractor_calls_for"), "pending_before": r.get("pending_before_labels")}
         for r in late})
c.check("U. each scheduled consolidation turn adds its own pending intent: the turn makes one extraction request, its "
        "bundle write (no candidates) raises CaptureRecoveryUnsupportedError and its intent is pending, not excluded",
        all(r.get("scheduled_turn_provider_extractions") == 1
            and r.get("scheduled_turn_intents") == [{"capture_state": "pending", "has_result": False}]
            and len(r.get("scheduled_turn_bundle_writes", [])) == 1
            and "CaptureRecoveryUnsupportedError" in str(r["scheduled_turn_bundle_writes"][0].get("raised"))
            for r in ur),
        [{k: r.get(k) for k in ("run", "scheduled_turn_intents", "scheduled_turn_provider_extractions")} for r in ur])
c.check("U and S. the scheduled turn's request meta carries memory_consolidation_job 'true', while the brain's "
        "capture_is_excluded receives metadata without that key and returns False",
        all(r.get("submit_meta") and all(m.get("memory_consolidation_job") == "true" for m in r["submit_meta"])
            and r.get("brain_capture_is_excluded_calls")
            and all(x.get("memory_consolidation_job") is None and x.get("cron_run_id_present") is True
                    and x.get("returned") is False for x in r["brain_capture_is_excluded_calls"])
            for r in ur + sr),
        {"U_run1": {k: ur[0].get(k) for k in ("submit_meta", "brain_capture_is_excluded_calls")} if ur else None,
         "S_run1": {k: sr[0].get(k) for k in ("submit_meta", "brain_capture_is_excluded_calls")} if sr else None})
c.check(f"S. control (built-in store, apply_capture_bundle callable): the user turn's capture succeeds, every one of the "
        f"{RUNS} runs scans 0 with no extractor call and no provider request inside recovery; each scheduled turn's own "
        "bundle commits (succeeded_no_output) and nothing stays pending",
        s.get("capture_capability_before_runs", {}).get("has_callable_apply_capture_bundle") is True
        and s.get("user_turn", {}).get("memory_capture_state") == "succeeded" and len(sr) == RUNS
        and all(r.get("recovery") == [[0, 0, 0]] and r.get("extractor_calls_for") == []
                and r.get("recovery_provider_extractions") == 0 and r.get("pending_before") == 0
                and r.get("pending_after") == 0 and r.get("state") == "finished"
                and r.get("scheduled_turn_provider_extractions") == 1
                and [w.get("disposition") for w in r.get("scheduled_turn_bundle_writes", [])] == ["succeeded_no_output"]
                for r in sr),
        [{k: r.get(k) for k in ("run", "recovery", "pending_after", "scheduled_turn_bundle_writes")} for r in sr])
hosts = sorted({h for r in (u, s) for h in r.get("provider_hosts", [])})
c.check("Offline provider: each case's openai base URL is the scripted server on 127.0.0.1",
        hosts == ["127.0.0.1"] and all(isinstance(r.get("provider_requests_total"), int)
                                       and r["provider_requests_total"] > 0 for r in (u, s)),
        {"hosts": hosts, "requests": {"U": u.get("provider_requests_total"), "S": s.get("provider_requests_total")}})


def table(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"run": r["run"], "pending_before": r.get("pending_before"),
             "scanned": (r.get("recovery") or [[None]])[0][0],
             "recovery_extraction_requests": r.get("recovery_provider_extractions"),
             "scheduled_turn_extraction_requests": r.get("scheduled_turn_provider_extractions"),
             "pending_after": r.get("pending_after")} for r in runs]


totals = {name: {"recovery_extraction_requests": sum(r.get("recovery_provider_extractions") or 0 for r in runs),
                 "scheduled_turn_extraction_requests": sum(r.get("scheduled_turn_provider_extractions") or 0
                                                           for r in runs)}
          for name, runs in (("U", ur), ("S", sr))}
write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "table": {"U": table(ur), "S": table(sr)}, "totals": totals, "observed": observed})
print(json.dumps({"table_U": table(ur), "totals": totals}, indent=None))
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
