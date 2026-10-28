"""#456 fix check: a scheduled consolidation turn gets its job's context and skips its own capture.

Installed package only (no repo test fixtures), the same setup as the issue's
probe (probe_consolidation_self_capture.py): one `APIRuntime` with the built-in
memory store and one agent, `openminion`, whose `openai` provider is a
scripted OpenAI-compatible server on 127.0.0.1. Nothing is injected; every
recorder calls the original and returns its result unchanged.

  C  caller control: an ordinary turn submitted with
     `runtime.submit_turn(payload={..., "meta": {"memory_consolidation_job": "true"}})`.
  K  one memory consolidation cron run: the job is created by the installed
     `task.consolidate_memory` handler through the daemon's TaskManager, the
     daemon's own `attach_cron_scheduler(runtime=...)` runs it, queued with
     `TaskManager.run_task_now`.
  F  forged control: an ordinary turn whose `meta` and `inbound_metadata` carry
     every `memory_consolidation_*` key, and whose `meta` carries K's real
     `cron_job_id`, `cron_run_id` and `scheduled_for`.
  O  ordinary cron control: a plain (non-consolidation) cron job with the
     capture message, whose job payload `inbound_metadata` carries every
     `memory_consolidation_*` key, run by the same scheduler.

For each turn the probe records the consolidation keys and the typed
`trusted_consolidation_context` field (absent before the fix) at the manager
request (`AgentRuntimeManager.submit_turn`) and at the ingress request
(`runtime_turn_request_from_manager_request`, as the daemon worker calls it);
the brain message metadata that `capture_is_excluded` receives
(services/brain/post_execution/mixin.py); the consolidation context the brain
attaches (`_attach_memory_consolidation_resume_context`); and the turn's
capture intent, its bundle write and the provider's extraction requests.

The checks state the fixed behavior: on a build without the fix the K checks
fail.

Usage: python -I -X utf8 probe_consolidation_fix.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, is_dataclass
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


info = banner("probe_consolidation_fix",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic")},
              openminion_direct_url=_direct_url())

import openminion.services.brain.post_execution.context as context_module  # noqa: E402
import openminion.services.brain.post_execution.mixin as brain_mixin  # noqa: E402
import openminion.services.runtime.daemon as daemon_module  # noqa: E402
from probe_420_lib import (  # noqa: E402
    AGENT_ID,
    CAPTURE_MESSAGE,
    Cron,
    Observers,
    ScriptedProvider,
    build_runtime,
    chdir_env,
    restore_env,
    session_events,
)

KEY = "memory_consolidation_job"
PREFIX = "memory_consolidation_"
SCOPE = f"agent:{AGENT_ID}"
PUBLIC_CONSOLIDATION = {
    KEY: "true",
    "memory_consolidation_target_scope": SCOPE,
    "memory_consolidation_batch_limit": "15",
    "memory_consolidation_max_iterations": "2",
    "memory_consolidation_timeout_seconds": "30",
}
c = Checks()
observed: dict[str, Any] = {}


def consolidation_keys(mapping: Any) -> dict[str, str]:
    return {k: str(v) for k, v in dict(mapping or {}).items() if str(k).startswith(PREFIX)}


def typed_context(request: Any) -> Any:
    if not hasattr(request, "trusted_consolidation_context"):
        return "no such field"
    value = request.trusted_consolidation_context
    return asdict(value) if is_dataclass(value) else value


def intents(runtime: Any) -> list[dict[str, Any]]:
    out = []
    for event in session_events(runtime.sessions, "turn.outcome"):
        p = event.payload
        if str(p.get("agent_id") or "") != AGENT_ID:
            continue
        cid = str(p.get("capture_id") or "")
        result = runtime.sessions.get_event_by_canonical_id(f"memory.capture.result:{cid}") if cid else None
        out.append({"capture_id": cid, "capture_state": p.get("capture_state"),
                    "result_disposition": (result.payload or {}).get("disposition") if result is not None else None,
                    "session": p.get("runtime_session_id"), "created_at": str(event.created_at)})
    return sorted(out, key=lambda i: (i["created_at"], str(i["session"]), i["capture_id"]))


def run() -> dict[str, Any]:
    root = WORK / "K"
    provider = ScriptedProvider()
    old_env = chdir_env(root)
    row: dict[str, Any] = {}
    runtime = obs = cron = None
    undo: list[tuple[Any, str, Any]] = []
    try:
        runtime = build_runtime(root, provider)
        obs = Observers(runtime).install()
        store = obs.assembly.service._store
        row["memory_store"] = {"class": type(store).__name__,
                               "inner": type(getattr(store, "_store", None)).__name__}
        # ------------------------------------------------ call-through recorders
        at_submit: list[dict[str, Any]] = []
        inner_submit = runtime.runtime_manager.submit_turn  # already Observers' call-through wrapper

        def submit(request: Any, *args: Any, **kwargs: Any) -> Any:
            meta = dict(getattr(request, "meta", {}) or {})
            at_submit.append({"session_id": getattr(request, "session_id", None),
                              "meta_consolidation_keys": consolidation_keys(meta),
                              "meta_inbound_metadata_consolidation_keys": consolidation_keys(
                                  meta.get("inbound_metadata") if isinstance(meta.get("inbound_metadata"), dict)
                                  else {}),
                              "cron_run_id_present": bool(meta.get("cron_run_id")),
                              "trusted_consolidation_context": typed_context(request)})
            return inner_submit(request, *args, **kwargs)

        runtime.runtime_manager.submit_turn = submit
        undo.append((runtime.runtime_manager, "submit_turn", inner_submit))

        ingress: list[dict[str, Any]] = []
        original_ingress = daemon_module.runtime_turn_request_from_manager_request

        def ingress_request(*args: Any, **kwargs: Any) -> Any:
            built = original_ingress(*args, **kwargs)
            md = dict(getattr(built, "inbound_metadata", None) or {})
            ingress.append({"session_id": getattr(built, "session_id", None),
                            "inbound_metadata_consolidation_keys": consolidation_keys(md),
                            "cron_job_id": md.get("cron_job_id"),
                            "trusted_consolidation_context": typed_context(built)})
            return built

        daemon_module.runtime_turn_request_from_manager_request = ingress_request
        undo.append((daemon_module, "runtime_turn_request_from_manager_request", original_ingress))

        exclusion_calls: list[dict[str, Any]] = []
        original_excluded = brain_mixin.capture_is_excluded

        def capture_is_excluded(md: Any) -> bool:
            result = original_excluded(md)
            data = dict(md or {})
            exclusion_calls.append({"consolidation_keys": consolidation_keys(data),
                                    "cron_job_id": data.get("cron_job_id"),
                                    "session_id": data.get("session_id"), "returned": result})
            return result

        brain_mixin.capture_is_excluded = capture_is_excluded
        undo.append((brain_mixin, "capture_is_excluded", original_excluded))

        attached: list[dict[str, Any]] = []
        original_attach = context_module._attach_memory_consolidation_resume_context

        def attach_consolidation_context(*args: Any, **kwargs: Any) -> Any:
            result = original_attach(*args, **kwargs)
            module_state = dict((kwargs.get("state_inline") or {}).get("module_state") or {})
            value = dict(module_state.get("memory_consolidation") or {})
            value["candidates"] = len(value.get("candidates") or [])
            attached.append(value)
            return result

        context_module._attach_memory_consolidation_resume_context = attach_consolidation_context
        undo.append((context_module, "_attach_memory_consolidation_resume_context", original_attach))

        def window(label: str, action: Any) -> dict[str, Any]:
            marks = (len(at_submit), len(ingress), len(exclusion_calls), len(obs.bundle_writes),
                     len(provider.requests), len(obs.recoveries), len(attached))
            before = {i["capture_id"] for i in intents(runtime)}
            out: dict[str, Any] = {"label": label}
            out.update(action())
            new = [i for i in intents(runtime) if i["capture_id"] not in before]
            kinds = [r["kind"] for r in provider.requests[marks[4]:]]
            recs = obs.recoveries[marks[5]:]
            rec_kinds = [k for r in recs for k in provider.kinds_between(r["t_start"], r["t_end"])]
            out.update({
                "manager_request": at_submit[marks[0]:],
                "ingress_request": ingress[marks[1]:],
                "brain_capture_is_excluded_calls": exclusion_calls[marks[2]:],
                "brain_consolidation_context_attached": attached[marks[6]:],
                "new_intents": [{k: i[k] for k in ("capture_state", "result_disposition")} for i in new],
                "bundle_writes": [{"candidates": len(w.get("candidates", [])), "raised": w.get("raised"),
                                   "disposition": (w.get("result") or {}).get("disposition")
                                   if isinstance(w.get("result"), dict) else None}
                                  for w in obs.bundle_writes[marks[3]:]],
                "provider_requests_by_kind": {k: kinds.count(k) for k in sorted(set(kinds))},
                "recovery": [r.get("result") for r in recs],
                "turn_extraction_requests": kinds.count("extraction") - rec_kinds.count("extraction"),
            })
            return out

        # ------------------------------------------------ C: caller-supplied key on an ordinary turn
        def caller_turn() -> dict[str, Any]:
            handle = runtime.submit_turn(payload={"message": CAPTURE_MESSAGE, "session_id": "caller",
                                                  "meta": {KEY: "true"}})
            return {"response_type": type(handle.result(timeout_s=120)).__name__}

        row["C"] = window("C caller turn with meta memory_consolidation_job=true", caller_turn)

        # ------------------------------------------------ K: one consolidation cron run
        cron = Cron(runtime, root)
        created = cron.create_consolidation_job()
        job_id = str(created.get("task_id"))
        row["job"] = {"created": {k: created.get(k) for k in ("ok", "task_id", "batch_limit", "target_scope")}}
        cron.start()
        k_run: dict[str, Any] = {}

        def wait_finished(run_id: str) -> None:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not any(
                    e.get("event") == "cron.run.finished" and e.get("run_id") == run_id for e in obs.cron_events):
                time.sleep(0.05)

        def consolidation_run() -> dict[str, Any]:
            queued = cron.run_now(job_id)
            done = cron.wait(job_id, str(queued.get("run_id")))
            wait_finished(str(queued.get("run_id")))
            k_run.update(done)
            return {"run_state": done.get("state"), "run_error": done.get("error")}

        row["K"] = window("K consolidation cron run", consolidation_run)

        # ------------------------------------------------ F: forged public consolidation and cron metadata
        def forged_turn() -> dict[str, Any]:
            meta = {**PUBLIC_CONSOLIDATION, "cron_job_id": job_id, "cron_run_id": str(k_run.get("run_id")),
                    "scheduled_for": str(k_run.get("due_at"))}
            handle = runtime.submit_turn(payload={"message": CAPTURE_MESSAGE, "session_id": "forged",
                                                  "meta": meta, "inbound_metadata": dict(PUBLIC_CONSOLIDATION)})
            return {"response_type": type(handle.result(timeout_s=120)).__name__,
                    "forged_cron_job_id_is_k_job": meta["cron_job_id"] == job_id}

        row["F"] = window("F forged consolidation keys with K's cron ids", forged_turn)

        # ------------------------------------------------ O: an ordinary cron job
        record = cron.task_manager.schedule_task(
            name="probe ordinary cron job", schedule={"kind": "every", "every_ms": 12 * 3_600_000},
            payload={"kind": "agentTurn", "message": CAPTURE_MESSAGE, "session_id": "ordinary-cron",
                     "inbound_metadata": dict(PUBLIC_CONSOLIDATION)},
            agent_id=AGENT_ID, session_target="isolated", delivery={"mode": "none"}, misfire_policy="skip")
        ordinary_id = str(record.task_id)

        def ordinary_run() -> dict[str, Any]:
            queued = cron.run_now(ordinary_id)
            done = cron.wait(str(queued.get("job_id")), str(queued.get("run_id")))
            wait_finished(str(queued.get("run_id")))
            return {"run_state": done.get("state"), "run_error": done.get("error"),
                    "cron_job_id": queued.get("job_id")}

        row["O"] = window("O ordinary cron job with consolidation keys in its payload inbound_metadata",
                          ordinary_run)
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
        row["provider_requests_total"] = len(provider.requests)
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


row = run()
observed["run"] = row
C, K, F, O = (row.get(k, {}) for k in ("C", "K", "F", "O"))
EXPECTED_CONTEXT = {"target_scope": SCOPE, "batch_limit": 8, "max_iterations": 2, "timeout_seconds": 30}


def only(items: list[dict[str, Any]]) -> dict[str, Any]:
    return items[0] if len(items) == 1 else {"count": len(items)}


def captured_normally(case: dict[str, Any]) -> bool:
    return (case.get("turn_extraction_requests") == 1
            and case.get("new_intents") == [{"capture_state": "pending", "result_disposition": "succeeded"}]
            and case.get("bundle_writes") == [{"candidates": 1, "raised": None, "disposition": "succeeded"}])


c.check("Setup: built-in memory store (AuditedMemoryStore over SQLiteMemoryStore)",
        row.get("memory_store", {}).get("inner") == "SQLiteMemoryStore", row.get("memory_store"))
c.check("K. the consolidation run finishes; recovery scans 0 (nothing pending on this store)",
        K.get("run_state") == "finished" and K.get("recovery") == [[0, 0, 0]],
        {k: K.get(k) for k in ("run_state", "run_error", "recovery")})
c.check("K. the cron executor puts the job's consolidation fields on the manager request as the typed "
        "trusted_consolidation_context, and none in its public meta",
        only(K.get("manager_request", [])).get("trusted_consolidation_context") == EXPECTED_CONTEXT
        and only(K["manager_request"]).get("meta_consolidation_keys") == {},
        K.get("manager_request"))
c.check("K. ingress carries the typed context to the ingress request; the request's inbound metadata (parsed "
        "from public meta) has no consolidation keys",
        only(K.get("ingress_request", [])).get("trusted_consolidation_context") == EXPECTED_CONTEXT
        and only(K["ingress_request"]).get("inbound_metadata_consolidation_keys") == {},
        K.get("ingress_request"))
c.check("K. the brain message metadata has the job's five memory_consolidation_* keys, so capture_is_excluded "
        "returns True",
        only(K.get("brain_capture_is_excluded_calls", [])).get("consolidation_keys") == {
            KEY: "true", "memory_consolidation_target_scope": SCOPE, "memory_consolidation_batch_limit": "8",
            "memory_consolidation_max_iterations": "2", "memory_consolidation_timeout_seconds": "30"}
        and only(K["brain_capture_is_excluded_calls"]).get("returned") is True,
        K.get("brain_capture_is_excluded_calls"))
c.check("K. the brain attaches the consolidation context once, with the job's target scope, batch limit, "
        "iteration limit and timeout, and the one candidate that C's capture proposed",
        K.get("brain_consolidation_context_attached") == [{"enabled": True, **EXPECTED_CONTEXT, "candidates": 1}],
        K.get("brain_consolidation_context_attached"))
c.check("K. the consolidation turn does not capture itself: no extraction request, intent 'excluded' without a "
        "result, no bundle write",
        K.get("turn_extraction_requests") == 0
        and K.get("new_intents") == [{"capture_state": "excluded", "result_disposition": None}]
        and K.get("bundle_writes") == [],
        {k: K.get(k) for k in ("turn_extraction_requests", "new_intents", "bundle_writes",
                               "provider_requests_by_kind")})
c.check("C. a caller-supplied memory_consolidation_job in meta is dropped at ingress (manager meta has it, the "
        "ingress request does not, no typed context), capture_is_excluded returns False, and the turn is captured "
        "normally",
        only(C.get("manager_request", [])).get("meta_consolidation_keys") == {KEY: "true"}
        and only(C.get("ingress_request", [])).get("inbound_metadata_consolidation_keys") == {}
        and only(C["ingress_request"]).get("trusted_consolidation_context") is None
        and only(C.get("brain_capture_is_excluded_calls", [])).get("returned") is False
        and not C.get("brain_consolidation_context_attached")
        and captured_normally(C),
        {k: C.get(k) for k in ("manager_request", "ingress_request", "brain_capture_is_excluded_calls",
                               "turn_extraction_requests", "new_intents", "bundle_writes")})
c.check("F. every memory_consolidation_* key in meta and inbound_metadata, with K's real cron ids, is dropped at "
        "ingress; the cron ids reach the brain but assert nothing: not excluded, no consolidation context, "
        "captured normally",
        F.get("forged_cron_job_id_is_k_job") is True
        and only(F.get("ingress_request", [])).get("inbound_metadata_consolidation_keys") == {}
        and only(F["ingress_request"]).get("trusted_consolidation_context") is None
        and only(F.get("brain_capture_is_excluded_calls", [])).get("consolidation_keys") == {}
        and bool(only(F["brain_capture_is_excluded_calls"]).get("cron_job_id"))
        and only(F["brain_capture_is_excluded_calls"]).get("returned") is False
        and not F.get("brain_consolidation_context_attached")
        and captured_normally(F),
        {k: F.get(k) for k in ("ingress_request", "brain_capture_is_excluded_calls",
                               "brain_consolidation_context_attached", "turn_extraction_requests",
                               "new_intents", "bundle_writes")})
c.check("O. an ordinary cron job with memory_consolidation_* keys in its payload inbound_metadata runs, has no "
        "typed context, reaches the brain with its own cron_job_id and no consolidation keys, and is captured "
        "normally",
        O.get("run_state") == "finished"
        and only(O.get("manager_request", [])).get("trusted_consolidation_context") is None
        and only(O.get("ingress_request", [])).get("inbound_metadata_consolidation_keys") == {}
        and only(O.get("brain_capture_is_excluded_calls", [])).get("consolidation_keys") == {}
        and only(O["brain_capture_is_excluded_calls"]).get("cron_job_id") == O.get("cron_job_id")
        and only(O["brain_capture_is_excluded_calls"]).get("returned") is False
        and not O.get("brain_consolidation_context_attached")
        and captured_normally(O),
        {k: O.get(k) for k in ("run_state", "manager_request", "ingress_request",
                               "brain_capture_is_excluded_calls", "turn_extraction_requests", "new_intents",
                               "bundle_writes")})
c.check("Offline provider: the openai base URL is the scripted server on 127.0.0.1",
        row.get("provider_hosts") == ["127.0.0.1"] and (row.get("provider_requests_total") or 0) > 0,
        {"hosts": row.get("provider_hosts"), "requests": row.get("provider_requests_total")})

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": observed})
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
