"""A scheduled memory consolidation turn captures itself: the trusted marker is lost before the brain.

Installed package only (no repo test fixtures). One `APIRuntime` with the
built-in memory store (AuditedMemoryStore over SQLiteMemoryStore) and one agent,
`openminion`, whose `openai` provider is a scripted OpenAI-compatible server on
127.0.0.1 (no external network). Nothing is injected; every recorder calls the
original and returns its result unchanged.

  C  caller control: an ordinary turn submitted with
     `runtime.submit_turn(payload={..., "meta": {"memory_consolidation_job": "true"}})`
     (a caller-supplied key, the path any SDK or daemon caller can use).
  K  one memory consolidation cron run: the job is created by the installed
     `task.consolidate_memory` handler through the daemon's TaskManager, the
     daemon's own `attach_cron_scheduler(runtime=...)` runs it, queued with
     `TaskManager.run_task_now`.

For each turn the probe records the key `memory_consolidation_job` at three
points: the manager request meta (`AgentRuntimeManager.submit_turn`), the
ingress request's inbound metadata
(`runtime_turn_request_from_manager_request`, as the daemon worker calls it),
and the brain message metadata that `capture_is_excluded` receives
(services/brain/post_execution/mixin.py). The same message metadata feeds the
brain's resume hints, so the probe also records whether the brain's memory
consolidation context is attached (`_attach_memory_consolidation_resume_context`
in services/brain/post_execution/context.py). It also records the turn's capture
intent, its bundle write and the provider's extraction requests.

Usage: python -I -X utf8 probe_consolidation_self_capture.py <work_dir> <out.json>
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


info = banner("probe_consolidation_self_capture",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic")},
              openminion_direct_url=_direct_url())

import openminion.services.brain.post_execution.context as context_module  # noqa: E402
import openminion.services.brain.post_execution.mixin as brain_mixin  # noqa: E402
import openminion.services.runtime.daemon as daemon_module  # noqa: E402
from openminion.modules.session.capture import capture_is_excluded as capture_is_excluded_fn  # noqa: E402
from openminion.services.gateway.turn.runtime import _extract_ephemeral_prompt_metadata  # noqa: E402
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
c = Checks()
observed: dict[str, Any] = {}


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
                    "session": p.get("runtime_session_id"), "root_turn_id": p.get("root_turn_id"),
                    "created_at": str(event.created_at)})
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
                               "inner": type(getattr(store, "_store", None)).__name__,
                               "apply_capture_bundle_callable": callable(getattr(store, "apply_capture_bundle", None))}
        # ------------------------------------------------ call-through recorders
        meta_at_submit: list[dict[str, Any]] = []
        inner_submit = runtime.runtime_manager.submit_turn  # already Observers' call-through wrapper

        def submit(request: Any, *args: Any, **kwargs: Any) -> Any:
            meta = dict(getattr(request, "meta", {}) or {})
            meta_at_submit.append({"session_id": getattr(request, "session_id", None),
                                   KEY: meta.get(KEY), "cron_run_id_present": bool(meta.get("cron_run_id"))})
            return inner_submit(request, *args, **kwargs)

        runtime.runtime_manager.submit_turn = submit
        undo.append((runtime.runtime_manager, "submit_turn", inner_submit))

        ingress: list[dict[str, Any]] = []
        original_ingress = daemon_module.runtime_turn_request_from_manager_request

        def ingress_request(*args: Any, **kwargs: Any) -> Any:
            built = original_ingress(*args, **kwargs)
            md = dict(getattr(built, "inbound_metadata", None) or {})
            ingress.append({"session_id": getattr(built, "session_id", None), "channel": getattr(built, "channel", None),
                            KEY: md.get(KEY), "cron_run_id_present": bool(md.get("cron_run_id"))})
            return built

        daemon_module.runtime_turn_request_from_manager_request = ingress_request
        undo.append((daemon_module, "runtime_turn_request_from_manager_request", original_ingress))

        exclusion_calls: list[dict[str, Any]] = []
        original_excluded = brain_mixin.capture_is_excluded

        def capture_is_excluded(md: Any) -> bool:
            result = original_excluded(md)
            data = dict(md or {})
            exclusion_calls.append({KEY: data.get(KEY), "cron_run_id_present": bool(data.get("cron_run_id")),
                                    "session_id": data.get("session_id"), "returned": result})
            return result

        brain_mixin.capture_is_excluded = capture_is_excluded
        undo.append((brain_mixin, "capture_is_excluded", original_excluded))

        # The same message metadata decides whether the brain gets its consolidation context
        # (services/brain/post_execution/context.py, _inject_resume_task_hints).
        hint_calls: list[dict[str, Any]] = []
        mixin_cls = brain_mixin.BrainBridgeTurnMixin
        original_hints = mixin_cls._inject_resume_task_hints

        def inject_resume_task_hints(self_: Any, *args: Any, **kwargs: Any) -> Any:
            data = dict(kwargs.get("inbound_metadata") or {})
            hint_calls.append({KEY: data.get(KEY), "cron_job_id_present": bool(data.get("cron_job_id")),
                               "session_id": kwargs.get("session_id")})
            return original_hints(self_, *args, **kwargs)

        mixin_cls._inject_resume_task_hints = inject_resume_task_hints
        undo.append((mixin_cls, "_inject_resume_task_hints", original_hints))
        consolidation_context_calls: list[dict[str, Any]] = []
        original_attach = context_module._attach_memory_consolidation_resume_context

        def attach_consolidation_context(*args: Any, **kwargs: Any) -> Any:
            consolidation_context_calls.append({"called": True})
            return original_attach(*args, **kwargs)

        context_module._attach_memory_consolidation_resume_context = attach_consolidation_context
        undo.append((context_module, "_attach_memory_consolidation_resume_context", original_attach))

        def window(label: str, action: Any) -> dict[str, Any]:
            marks = (len(meta_at_submit), len(ingress), len(exclusion_calls), len(obs.bundle_writes),
                     len(provider.requests), len(obs.recoveries), len(hint_calls),
                     len(consolidation_context_calls))
            before = {i["capture_id"] for i in intents(runtime)}
            out: dict[str, Any] = {"label": label}
            out.update(action())
            new = [i for i in intents(runtime) if i["capture_id"] not in before]
            kinds = [r["kind"] for r in provider.requests[marks[4]:]]
            recs = obs.recoveries[marks[5]:]
            rec_kinds = [k for r in recs for k in provider.kinds_between(r["t_start"], r["t_end"])]
            out.update({
                "meta_at_submit": meta_at_submit[marks[0]:],
                "ingress_inbound_metadata": ingress[marks[1]:],
                "brain_capture_is_excluded_calls": exclusion_calls[marks[2]:],
                "brain_resume_task_hints_calls": hint_calls[marks[6]:],
                "brain_consolidation_context_attached": len(consolidation_context_calls) - marks[7],
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
            response = handle.result(timeout_s=120)
            return {"response_type": type(response).__name__}

        row["C"] = window("C caller turn with meta memory_consolidation_job=true", caller_turn)

        # ------------------------------------------------ K: one consolidation cron run
        cron = Cron(runtime, root)
        created = cron.create_consolidation_job()
        job_id = str(created.get("task_id"))
        job = cron.store.get_cron_job(job_id) or {}
        row["job"] = {"created": {k: created.get(k) for k in ("ok", "task_id", "batch_limit", "target_scope")},
                      "payload_consolidation": (job.get("payload") or {}).get("_openminion_memory_consolidation")}
        cron.start()

        def consolidation_run() -> dict[str, Any]:
            queued = cron.run_now(job_id)
            done = cron.wait(job_id, str(queued.get("run_id")))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not any(
                    e.get("event") == "cron.run.finished" and e.get("run_id") == queued.get("run_id")
                    for e in obs.cron_events):
                time.sleep(0.05)
            return {"run_state": done.get("state"), "run_error": done.get("error")}

        row["K"] = window("K consolidation cron run", consolidation_run)
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
observed["direct_calls"] = {
    "capture_is_excluded({memory_consolidation_job: 'true'})": capture_is_excluded_fn({KEY: "true"}),
    "capture_is_excluded({})": capture_is_excluded_fn({}),
    "_metadata_bool({memory_consolidation_job: 'true'}, memory_consolidation_job) (resume hints)":
        context_module._metadata_bool({KEY: "true"}, KEY),
    "_extract_ephemeral_prompt_metadata({memory_consolidation_job, cron_job_id, cron_run_id, scheduled_for})":
        _extract_ephemeral_prompt_metadata({KEY: "true", "cron_job_id": "j", "cron_run_id": "r",
                                            "scheduled_for": "2026-10-28T00:00:00+00:00"}),
}
C = row.get("C", {})
K = row.get("K", {})


def only(items: list[dict[str, Any]]) -> dict[str, Any]:
    return items[0] if len(items) == 1 else {"count": len(items)}


c.check("Setup: built-in memory store (AuditedMemoryStore over SQLiteMemoryStore) with a callable apply_capture_bundle",
        row.get("memory_store", {}).get("inner") == "SQLiteMemoryStore"
        and row["memory_store"].get("apply_capture_bundle_callable") is True, row.get("memory_store"))
c.check("Direct calls: capture_is_excluded returns True for memory_consolidation_job 'true' (False without it), and so does the resume hints' _metadata_bool; the "
        "gateway's _extract_ephemeral_prompt_metadata keeps cron_job_id, cron_run_id and scheduled_for and drops "
        "memory_consolidation_job",
        observed["direct_calls"]["capture_is_excluded({memory_consolidation_job: 'true'})"] is True
        and observed["direct_calls"]["capture_is_excluded({})"] is False
        and observed["direct_calls"][
            "_metadata_bool({memory_consolidation_job: 'true'}, memory_consolidation_job) (resume hints)"] is True
        and sorted(observed["direct_calls"][
            "_extract_ephemeral_prompt_metadata({memory_consolidation_job, cron_job_id, cron_run_id, scheduled_for})"
        ]) == ["cron_job_id", "cron_run_id", "scheduled_for"],
        observed["direct_calls"])
c.check("K. the consolidation run finishes; recovery scans 0 (nothing pending on this store)",
        K.get("run_state") == "finished" and K.get("recovery") == [[0, 0, 0]],
        {k: K.get(k) for k in ("run_state", "run_error", "recovery")})
c.check("K. the marker is set and survives ingress: memory_consolidation_job is 'true' in the manager request meta and "
        "in the ingress request's inbound metadata",
        only(K.get("meta_at_submit", [])).get(KEY) == "true"
        and only(K.get("ingress_inbound_metadata", [])).get(KEY) == "true",
        {k: K.get(k) for k in ("meta_at_submit", "ingress_inbound_metadata")})
c.check("K. the marker is lost before the brain: capture_is_excluded receives message metadata with cron_run_id but "
        "without memory_consolidation_job, and returns False",
        only(K.get("brain_capture_is_excluded_calls", [])).get(KEY) is None
        and only(K["brain_capture_is_excluded_calls"]).get("cron_run_id_present") is True
        and only(K["brain_capture_is_excluded_calls"]).get("returned") is False,
        K.get("brain_capture_is_excluded_calls"))
c.check("K. the same metadata reaches the brain's resume hints without memory_consolidation_job (cron_job_id present), "
        "so the brain's memory consolidation context is never attached (_attach_memory_consolidation_resume_context "
        "is not called)",
        only(K.get("brain_resume_task_hints_calls", [])).get(KEY) is None
        and only(K["brain_resume_task_hints_calls"]).get("cron_job_id_present") is True
        and K.get("brain_consolidation_context_attached") == 0,
        {k: K.get(k) for k in ("brain_resume_task_hints_calls", "brain_consolidation_context_attached")})
c.check("K. so the consolidation turn captures itself: it makes one extraction request, its intent is 'pending' (not "
        "'excluded') and its bundle (no candidates) commits succeeded_no_output with a memory.capture.result",
        K.get("turn_extraction_requests") == 1
        and K.get("new_intents") == [{"capture_state": "pending", "result_disposition": "succeeded_no_output"}]
        and K.get("bundle_writes") == [{"candidates": 0, "raised": None, "disposition": "succeeded_no_output"}],
        {k: K.get(k) for k in ("turn_extraction_requests", "new_intents", "bundle_writes",
                               "provider_requests_by_kind")})
c.check("C. a caller-supplied memory_consolidation_job reaches the manager request meta and the ingress inbound "
        "metadata, but not the brain: capture_is_excluded gets no such key and returns False",
        only(C.get("meta_at_submit", [])).get(KEY) == "true"
        and only(C.get("ingress_inbound_metadata", [])).get(KEY) == "true"
        and only(C.get("brain_capture_is_excluded_calls", [])).get(KEY) is None
        and only(C["brain_capture_is_excluded_calls"]).get("returned") is False,
        {k: C.get(k) for k in ("meta_at_submit", "ingress_inbound_metadata", "brain_capture_is_excluded_calls")})
c.check("C. the caller's turn is captured normally: one extraction request, intent 'pending' with a succeeded result "
        "(one candidate)",
        C.get("turn_extraction_requests") == 1
        and C.get("new_intents") == [{"capture_state": "pending", "result_disposition": "succeeded"}]
        and C.get("bundle_writes") == [{"candidates": 1, "raised": None, "disposition": "succeeded"}],
        {k: C.get(k) for k in ("turn_extraction_requests", "new_intents", "bundle_writes")})
c.check("Offline provider: the openai base URL is the scripted server on 127.0.0.1",
        row.get("provider_hosts") == ["127.0.0.1"] and (row.get("provider_requests_total") or 0) > 0,
        {"hosts": row.get("provider_hosts"), "requests": row.get("provider_requests_total")})

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": observed})
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
