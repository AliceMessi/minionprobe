"""Helpers for probe_456*.py (installed package only, no repo test fixtures).

They reuse the #449 harness (probe_449_lib.py, byte-identical to the
probe_420_lib.py of the original #456 report, and probe_449_ext.py) and add
call-through recorders for where the consolidation context travels, as in the
#456 report's probe, plus what 0.1.15 adds:

  1. the manager request (`AgentRuntimeManager.submit_turn`): its `meta` and,
     where the package has it, `trusted_task_context`;
  2. the ingress request (`runtime_turn_request_from_manager_request`, as the
     daemon worker calls it): its `inbound_metadata` and `trusted_task_context`;
  3. the brain: the message metadata `capture_is_excluded` receives and returns
     on, the metadata `_inject_resume_task_hints` receives, and each call of
     `_attach_memory_consolidation_resume_context` with the consolidation
     state it leaves in the working state (target scope, batch limit,
     iteration and timeout limits, candidates);
  4. the provider: whether a request's text contains the captured candidate's
     title, which the model sees only through the consolidation context;
  5. the task owner: every `get_task_by_job` call on a TaskManager, with the
     task kind it returned and whether it was the probe's own TaskManager
     object or another one (the daemon's, built from the persisted database).

Every recorder calls the original and returns or raises exactly what it did.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from probe_449_lib import CAPTURE_ITEM, _all_text

KEYS = ("memory_consolidation_job", "memory_consolidation_target_scope", "memory_consolidation_batch_limit",
        "memory_consolidation_max_iterations", "memory_consolidation_timeout_seconds")
RESERVED_PAYLOAD_KEY = "_openminion_memory_consolidation"
CANDIDATE_TITLE = CAPTURE_ITEM["title"]


def consolidation_keys(mapping: Any) -> dict[str, Any]:
    data = dict(mapping or {})
    return {k: data[k] for k in KEYS if k in data}


def forged_keys(job_id: str) -> dict[str, str]:
    """What a caller could put in public metadata to look like a scheduled consolidation turn."""
    return {"memory_consolidation_job": "true", "memory_consolidation_target_scope": "agent:openminion",
            "memory_consolidation_batch_limit": "8", "memory_consolidation_max_iterations": "2",
            "memory_consolidation_timeout_seconds": "30", "cron_job_id": job_id, "cron_run_id": "forged-run",
            "scheduled_for": "2026-10-29T00:00:00+00:00"}


class TrustRecorder:
    def __init__(self, runtime: Any, provider: Any, probe_task_manager: Any = None) -> None:
        self.runtime = runtime
        self.provider = provider
        self.probe_task_manager = probe_task_manager
        self.submits: list[dict[str, Any]] = []
        self.ingress: list[dict[str, Any]] = []
        self.exclusions: list[dict[str, Any]] = []
        self.hints: list[dict[str, Any]] = []
        self.contexts: list[dict[str, Any]] = []
        self.provider_marks: list[dict[str, Any]] = []
        self.task_lookups: list[dict[str, Any]] = []
        self._undo: list[tuple[Any, str, Any, bool]] = []
        self.lock = threading.Lock()

    def _set(self, obj: Any, name: str, value: Any, *, instance: bool) -> None:
        original = getattr(obj, name)
        had_own = instance and name in getattr(obj, "__dict__", {})
        setattr(obj, name, value)
        self._undo.append((obj, name, original, had_own or not instance))

    def install(self) -> "TrustRecorder":
        import openminion.services.brain.post_execution.context as context_module
        import openminion.services.brain.post_execution.mixin as brain_mixin
        import openminion.services.runtime.daemon as daemon_module

        rec = self
        manager = self.runtime.runtime_manager
        inner_submit = manager.submit_turn

        def submit(request: Any, *args: Any, **kwargs: Any) -> Any:
            trusted = getattr(request, "trusted_task_context", "absent")
            rec.submits.append({"session_id": getattr(request, "session_id", None),
                                "meta_consolidation_keys": consolidation_keys(getattr(request, "meta", None)),
                                "meta_has_cron_job_id": bool(dict(getattr(request, "meta", None) or {})
                                                             .get("cron_job_id")),
                                "trusted_task_context": trusted if trusted in (None, "absent") else dict(trusted)})
            return inner_submit(request, *args, **kwargs)

        self._set(manager, "submit_turn", submit, instance=True)

        original_ingress = daemon_module.runtime_turn_request_from_manager_request

        def ingress(*args: Any, **kwargs: Any) -> Any:
            try:
                built = original_ingress(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                rec.ingress.append({"raised": f"{type(exc).__name__}: {exc}"})
                raise
            trusted = getattr(built, "trusted_task_context", "absent")
            md = dict(getattr(built, "inbound_metadata", None) or {})
            rec.ingress.append({"session_id": getattr(built, "session_id", None),
                                "inbound_metadata_consolidation_keys": consolidation_keys(md),
                                "inbound_metadata_has_cron_job_id": bool(md.get("cron_job_id")),
                                "trusted_task_context": trusted if trusted in (None, "absent") else dict(trusted)})
            return built

        self._set(daemon_module, "runtime_turn_request_from_manager_request", ingress, instance=False)

        original_excluded = brain_mixin.capture_is_excluded

        def capture_is_excluded(md: Any) -> bool:
            result = original_excluded(md)
            data = dict(md or {})
            rec.exclusions.append({"session_id": data.get("session_id"),
                                   "consolidation_keys": consolidation_keys(data),
                                   "cron_job_id_present": bool(data.get("cron_job_id")), "returned": result,
                                   "t": time.monotonic()})
            return result

        self._set(brain_mixin, "capture_is_excluded", capture_is_excluded, instance=False)

        mixin_cls = brain_mixin.BrainBridgeTurnMixin
        original_hints = mixin_cls._inject_resume_task_hints

        def hints(self_: Any, *args: Any, **kwargs: Any) -> Any:
            data = dict(kwargs.get("inbound_metadata") or {})
            rec.hints.append({"session_id": kwargs.get("session_id"), "consolidation_keys": consolidation_keys(data),
                              "cron_job_id_present": bool(data.get("cron_job_id"))})
            return original_hints(self_, *args, **kwargs)

        self._set(mixin_cls, "_inject_resume_task_hints", hints, instance=False)

        original_attach = context_module._attach_memory_consolidation_resume_context

        def attach(*args: Any, **kwargs: Any) -> Any:
            out = original_attach(*args, **kwargs)
            state = kwargs.get("state_inline") if "state_inline" in kwargs else (args[0] if args else None)
            module_state = context_module._module_state(state) if isinstance(state, dict) else {}
            mc = dict(module_state.get("memory_consolidation") or {})
            candidates = list(mc.get("candidates") or [])
            rec.contexts.append({
                "inbound_consolidation_keys": consolidation_keys(kwargs.get("inbound_metadata")),
                "state": {k: mc.get(k) for k in ("enabled", "target_scope", "batch_limit", "max_iterations",
                                                 "timeout_seconds")},
                "candidate_count": len(candidates),
                "candidate_fields": sorted(candidates[0].keys()) if candidates and isinstance(candidates[0], dict)
                else None,
                "candidates": [{k: (c.get(k) if isinstance(c, dict) else None) for k in ("candidate_id", "title")}
                               for c in candidates],
                "t": time.monotonic()})
            return out

        self._set(context_module, "_attach_memory_consolidation_resume_context", attach, instance=False)

        provider = self.provider
        original_respond = provider.respond

        def respond(body: Any, path: str) -> Any:
            import re

            text = _all_text(body)
            choice = body.get("tool_choice") if isinstance(body, dict) else None
            forced = str((choice.get("function") or {}).get("name", "")) if isinstance(choice, dict) else ""
            purpose = re.search(r"purpose: (\w+)", text)
            with rec.lock:
                rec.provider_marks.append({"t": time.monotonic(), "mentions_candidate": CANDIDATE_TITLE in text,
                                           "forced_function": forced or None,
                                           "purpose": purpose.group(1) if purpose else None})
            return original_respond(body, path)

        self._set(provider, "respond", respond, instance=True)

        from openminion.modules.task import TaskManager

        cls = TaskManager
        original_lookup = cls.get_task_by_job

        def lookup(self_: Any, cron_job_id: str, *args: Any, **kwargs: Any) -> Any:
            task = original_lookup(self_, cron_job_id, *args, **kwargs)
            rec.task_lookups.append({"job_id": cron_job_id,
                                     "found": task is not None,
                                     "kind": (getattr(task, "metadata", None) or {}).get("kind")
                                     if task is not None else None,
                                     "probe_task_manager": self_ is rec.probe_task_manager,
                                     "thread": threading.current_thread().name})
            return task

        self._set(cls, "get_task_by_job", lookup, instance=False)
        return self

    def uninstall(self) -> None:
        for obj, name, original, restore in reversed(self._undo):
            if restore:
                setattr(obj, name, original)
            else:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
        self._undo.clear()

    def marks(self) -> dict[str, int]:
        return {k: len(getattr(self, k)) for k in ("submits", "ingress", "exclusions", "hints", "contexts",
                                                   "provider_marks", "task_lookups")}

    def since(self, marks: dict[str, int]) -> dict[str, list[dict[str, Any]]]:
        return {k: list(getattr(self, k)[v:]) for k, v in marks.items()}


def consolidation_jobs(store: Any) -> list[dict[str, Any]]:
    """Every cron job with the reserved consolidation payload, with its enabled flag."""
    out = []
    for job in store.list_cron_jobs(limit=1000):
        payload = job.get("payload") or {}
        if isinstance(payload, dict) and RESERVED_PAYLOAD_KEY in payload:
            out.append({"job_id": job.get("job_id"), "enabled": bool(job.get("enabled")),
                        "name": job.get("name")})
    return sorted(out, key=lambda j: str(j["job_id"]))


def task_view(task_manager: Any, task_id: str) -> dict[str, Any] | None:
    task = task_manager.get_task(task_id)
    if task is None:
        return None
    state = getattr(task, "state", None)
    return {"state": getattr(state, "value", state), "kind": (getattr(task, "metadata", None) or {}).get("kind"),
            "metadata_keys": sorted((getattr(task, "metadata", None) or {}).keys())}


# ---------------------------------------------------------------- one runtime home
class Home:
    """One APIRuntime on a runtime home, with the #449 harness's recorders and the TrustRecorder."""

    def __init__(self, root: Any, labels: dict[str, str], *, external: str | None = None, memory: Any = None) -> None:
        from probe_449_ext import StepRecorder
        from probe_449_lib import Cron, Observers, ScriptedProvider, build_runtime, chdir_env

        self.root, self.labels = root, labels
        self.provider = ScriptedProvider()
        self.old_env = chdir_env(root)
        self.runtime = self.obs = self.rec = self.cron = self.trust = None
        self.closed: dict[str, Any] = {}
        try:
            self.runtime = build_runtime(root, self.provider, external_adapter=external, memory_config=memory)
            self.obs = Observers(self.runtime).install()
            self.rec = StepRecorder(self.runtime, self.obs).install()
            self.cron = Cron(self.runtime, root)
            self.trust = TrustRecorder(self.runtime, self.provider, self.cron.task_manager).install()
            store = self.obs.assembly.service._store
            self.inner = getattr(store, "_store", None)
            backend = getattr(store, "_backend", None)
            self.classes = {"memory_store": type(store).__name__,
                            "memory_store_inner": None if self.inner is None else type(self.inner).__name__,
                            "memory_backend": None if backend is None else type(backend).__name__}
            target = backend if backend is not None else self.inner
            self.capable = callable(getattr(target, "apply_capture_bundle", None))
        except Exception:
            self.close()
            raise

    def memory_db(self) -> Any:
        from pathlib import Path

        db = getattr(self.inner, "db_path", None)
        return Path(str(db)) if db else None

    def observe(self, label: str, action: Any) -> dict[str, Any]:
        from probe_449_ext import count_steps, intents, steps_by_root

        rt = self.runtime
        before = intents(rt)
        known = {i["capture_id"] for i in before}
        self.rec.capture_roots = {i["capture_id"]: i["root_turn_id"] for i in before}
        smark, pmark, rmark = self.rec.mark(), len(self.provider.requests), len(self.obs.recoveries)
        tmarks = self.trust.marks()
        try:
            out = dict(action() or {})
        except Exception as exc:  # noqa: BLE001
            out = {"raised": f"{type(exc).__name__}: {exc}"}
        after = intents(rt)
        new = [i for i in after if i["capture_id"] not in known]
        for i in new:
            self.labels.setdefault(str(i["root_turn_id"]), out.pop("_label", None) or str(i["session"]))
        out.pop("_label", None)
        steps = self.rec.since(smark)
        recs = self.obs.recoveries[rmark:]
        windows = [(r["t_start"], r["t_end"]) for r in recs]
        in_rec = [k for s, e in windows for k in self.provider.kinds_between(s, e)]
        kinds = [r["kind"] for r in self.provider.requests[pmark:]]
        trust = self.trust.since(tmarks)
        marks = trust.pop("provider_marks")
        mentions = [m for m in marks if m["mentions_candidate"] and not any(s <= m["t"] <= e for s, e in windows)]
        purposes = [f"{m['forced_function'] or 'text'}/{m['purpose'] or '-'}" for m in marks]
        out.update({
            "label": label,
            "new_intents": [{"label": self.labels.get(str(i["root_turn_id"])), "capture_state": i["capture_state"],
                             "results": i["results"], "result_disposition": i["result_disposition"]} for i in new],
            "recovery": [r.get("result") for r in recs],
            "recovery_raised": [r.get("raised") for r in recs if r.get("raised")],
            "recovery_steps_by_intent": steps_by_root(steps, self.labels),
            "recovery_extraction_requests": in_rec.count("extraction"),
            "turn_extraction_requests": kinds.count("extraction") - in_rec.count("extraction"),
            "turn_own_bundle_writes": count_steps(steps, "bundle", recovery_only=False) - count_steps(steps, "bundle"),
            "provider_requests_by_kind": {k: kinds.count(k) for k in sorted(set(kinds))},
            "provider_requests_mentioning_candidate_outside_recovery": len(mentions),
            "provider_requests_forced_function_and_purpose": {p: purposes.count(p) for p in sorted(set(purposes))},
            **trust,
        })
        for t in ("exclusions", "contexts"):
            for item in out[t]:
                item.pop("t", None)
        return out

    def sdk_turn(self, session: str, message: str | None = None) -> dict[str, Any]:
        from probe_449_ext import turn
        from probe_449_lib import CAPTURE_MESSAGE

        from openminion import Agent

        with Agent(runtime=self.runtime) as agent:
            out = turn(lambda: agent.run(message or CAPTURE_MESSAGE, session_id=session, timeout_seconds=120))
        out["_label"] = session
        return out

    def submit(self, session: str, extra: dict[str, Any], message: str | None = None) -> dict[str, Any]:
        from probe_449_lib import CAPTURE_MESSAGE

        payload = {"message": message or CAPTURE_MESSAGE, "session_id": session, **extra}
        try:
            handle = self.runtime.submit_turn(payload=payload)
            response = handle.result(timeout_s=120)
        except Exception as exc:  # noqa: BLE001
            return {"raised": f"{type(exc).__name__}: {exc}", "_label": session}
        md = dict(getattr(response, "metadata", None) or {})
        return {"response_type": type(response).__name__, "memory_capture_state": md.get("memory_capture_state"),
                "_label": session}

    def run(self, job_id: str, label: str) -> dict[str, Any]:
        from probe_449_ext import run_job

        out = run_job(self.cron, self.obs, job_id)
        out["_label"] = label
        return out

    def trigger(self, job_id: str, label: str) -> dict[str, Any]:
        from probe_449_ext import wait_run

        run_id = self.cron.store.trigger_cron_run(job_id=job_id)
        out = wait_run(self.cron, self.obs, job_id, str(run_id))
        out["_label"] = label
        return out

    def close(self) -> dict[str, Any]:
        from probe_449_lib import restore_env

        if self.cron is not None:
            try:
                self.cron.stop()
            except Exception as exc:  # noqa: BLE001
                self.closed["scheduler_stop"] = f"{type(exc).__name__}: {exc}"
        for part in (self.trust, self.rec, self.obs):
            if part is not None:
                part.uninstall()
        if self.runtime is not None:
            try:
                self.runtime.close()
            except Exception as exc:  # noqa: BLE001
                self.closed["runtime_close"] = f"{type(exc).__name__}: {exc}"
            self.runtime = None
        restore_env(self.old_env)
        kinds = [r["kind"] for r in self.provider.requests]
        self.closed.update({"provider_requests_total": len(kinds),
                            "provider_requests_by_kind": {k: kinds.count(k) for k in sorted(set(kinds))},
                            "provider_hosts": sorted({self.provider.base_url.split("/")[2].split(":")[0]})})
        self.provider.close()
        return self.closed
