"""#456 on the installed package: newly created consolidation jobs carry trusted context, and nothing else can.

Installed package only (no repo test fixtures). Real `APIRuntime`s with one
agent, `openminion`, whose `openai` provider points at a scripted
OpenAI-compatible server on 127.0.0.1 (the harness of the #456 report). The
consolidation job is created by the installed `task.consolidate_memory`
handler through a TaskManager over the daemon's cron store and task database;
the daemon's own `attach_cron_scheduler(runtime=...)` executes it (with the
TaskManager it builds itself from the persisted database), and each run is
queued with `TaskManager.run_task_now`. probe_456_lib.TrustRecorder records the
consolidation keys at the manager request, the ingress request and the brain,
the consolidation context the brain builds, and the task lookups.

Home K, built-in SQLite memory store (capable):
  E   an earlier SDK turn whose own capture write is refused by the database (a
      BEFORE INSERT trigger on memory_capture_bundle_receipts, dropped right
      after the turn), so its intent stays pending;
  O   an ordinary SDK turn;
  J   a new consolidation job (task.consolidate_memory, batch_limit 8);
  FM, FI, FN  turns through `APIRuntime.submit_turn` whose public metadata
      carries every consolidation key plus J's real cron_job_id: in `meta`, in
      `inbound_metadata`, and in `meta.inbound_metadata`;
  K1, K2  two runs of J;
  C   an ordinary (non-consolidation) agentTurn cron job created through the
      raw cron API operation (`openminion.api.operations.cron.create_cron_job`,
      behind POST /v1/cron) whose payload carries the same keys in its
      `inbound_metadata`, triggered once;
  RAW the raw cron API operation asked to create a job with the reserved
      consolidation payload;
  ROW a job with the reserved consolidation payload written straight into the
      cron store (no task record: what a forged row, or a job from before
      the provenance marker, looks like), triggered once.
Home I, the bundled `reference-sqlite` backend (cannot commit a bundle):
  one SDK turn (pending, unsupported), a new job, two runs; then the same home
  reopened with the built-in store, two runs.

Usage: python -I -X utf8 probe_456.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize, write_result  # noqa: E402

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


info = banner("probe_456",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic", "httpx", "openai")})

from openminion.modules.memory.backends.external import register_reference_sqlite_backend  # noqa: E402
from probe_449_ext import (  # noqa: E402
    REFUSE_MESSAGE,
    create_job,
    intents,
    pending,
    sqlite_count,
    sqlite_trigger,
)
from probe_449_lib import sqlite_rows  # noqa: E402
from probe_456_lib import (  # noqa: E402
    CANDIDATE_TITLE,
    RESERVED_PAYLOAD_KEY,
    Home,
    consolidation_jobs,
    forged_keys,
    task_view,
)

c = Checks()
observed: dict[str, Any] = {}
EXPECTED_CONTEXT = {"memory_consolidation_job": "true", "memory_consolidation_target_scope": "agent:openminion",
                    "memory_consolidation_batch_limit": "8", "memory_consolidation_max_iterations": "2",
                    "memory_consolidation_timeout_seconds": "30"}
EXPECTED_STATE = {"enabled": True, "target_scope": "agent:openminion", "batch_limit": 8, "max_iterations": 2,
                  "timeout_seconds": 30}


def guarded(name: str, body: Any) -> dict[str, Any]:
    try:
        return body()
    except Exception as exc:  # noqa: BLE001
        import traceback

        print(f"{name}: case error {type(exc).__name__}: {exc}", flush=True)
        return {"case_error": f"{type(exc).__name__}: {exc}",
                "case_error_frames": [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                      for f in traceback.extract_tb(exc.__traceback__)][-8:]}


def show(row: dict[str, Any]) -> None:
    print(f"{row.get('label')}: state={row.get('state')} raised={row.get('raised')} summary={row.get('summary')!r} "
          f"recovery={row.get('recovery')} steps={row.get('recovery_steps_by_intent')} "
          f"new={row.get('new_intents')} turn_extractions={row.get('turn_extraction_requests')} "
          f"excl={[e.get('returned') for e in row.get('exclusions', [])]} contexts={len(row.get('contexts', []))}",
          flush=True)


# ---------------------------------------------------------------- home K
def home_k() -> dict[str, Any]:
    from openminion.api.operations import cron as cron_ops
    from openminion.modules.task import TaskManager
    from openminion.modules.task.constants import DEFAULT_INTEGRATED_SQLITE_SUBPATH

    labels: dict[str, str] = {}
    out: dict[str, Any] = {}
    h = Home(WORK / "K", labels)
    try:
        out["classes"], out["capable"] = h.classes, h.capable
        db = h.memory_db()
        out["trigger_on"] = sqlite_trigger(db, True)
        out["E"] = h.observe("E earlier turn", lambda: h.sdk_turn("earlier"))
        out["trigger_off"] = sqlite_trigger(db, False)
        out["O"] = h.observe("O ordinary turn", lambda: h.sdk_turn("ordinary"))
        created = create_job(h.cron, {"interval_hours": 12, "batch_limit": 8})
        job_id = str(created.get("task_id"))
        out["J_created"] = {k: created.get(k) for k in ("ok", "task_id", "batch_limit", "target_scope", "enabled")}
        fresh = TaskManager.from_cron_repository(
            h.cron.store, db_path=(Path(h.runtime.data_root) / DEFAULT_INTEGRATED_SQLITE_SUBPATH).resolve())
        out["J_task_probe_manager"] = task_view(h.cron.task_manager, job_id)
        out["J_task_fresh_manager_from_persisted_db"] = task_view(fresh, job_id)
        forged = forged_keys(job_id)
        out["FM"] = h.observe("FM forged meta", lambda: h.submit("forged-meta", {"meta": dict(forged)}))
        out["FI"] = h.observe("FI forged inbound_metadata",
                              lambda: h.submit("forged-inbound", {"inbound_metadata": dict(forged)}))
        out["FN"] = h.observe("FN forged meta.inbound_metadata",
                              lambda: h.submit("forged-nested", {"meta": {"inbound_metadata": dict(forged)}}))
        h.cron.start()
        out["K1"] = h.observe("K1 J run 1", lambda: h.run(job_id, "scheduled J run 1"))
        out["store_candidate_ids_after_K1"] = sorted(
            r[0] for r in sqlite_rows(db, "SELECT candidate_id FROM memory_candidates")) if db else None
        out["K2"] = h.observe("K2 J run 2", lambda: h.run(job_id, "scheduled J run 2"))
        earlier = next((i for i in intents(h.runtime) if labels.get(str(i["root_turn_id"])) == "earlier"), None)
        out["E_after"] = None if earlier is None else {k: earlier[k] for k in ("capture_state", "results")}
        out["E_receipts"] = sqlite_count(db, "memory_capture_bundle_receipts", "WHERE capture_id = ?",
                                         (earlier["capture_id"],)) if earlier else None

        def ordinary_cron() -> dict[str, Any]:
            body = {"name": "probe ordinary cron turn", "agent_id": "openminion",
                    "schedule": {"kind": "every", "every_ms": 12 * 3_600_000},
                    "payload": {"kind": "agentTurn", "message": "Please remember that I prefer tabs over spaces "
                                "for indentation.", "session_id": "cron-ordinary",
                                "inbound_metadata": dict(forged), "meta": dict(forged)}}
            cid = cron_ops.create_cron_job(runtime=h.runtime, body=body)
            out["C_job_id"] = cid
            run_id = cron_ops.trigger_cron_job(runtime=h.runtime, job_id=cid)
            from probe_449_ext import wait_run

            r = wait_run(h.cron, h.obs, cid, str(run_id))
            r["_label"] = "cron-ordinary"
            return r

        out["C"] = h.observe("C ordinary cron job with forged inbound_metadata", ordinary_cron)
        jobs_before = consolidation_jobs(h.cron.store)

        def raw_api() -> dict[str, Any]:
            body = {"name": "probe raw consolidation", "agent_id": "openminion",
                    "schedule": {"kind": "every", "every_ms": 12 * 3_600_000},
                    "payload": {"kind": "agentTurn", "message": "Review the provided memory candidates.",
                                "session_id": "raw-consolidation",
                                RESERVED_PAYLOAD_KEY: {"batch_limit": 8, "max_iterations": 2, "timeout_seconds": 30,
                                                       "target_scope": "agent:openminion"}}}
            try:
                return {"created_job_id": cron_ops.create_cron_job(runtime=h.runtime, body=body)}
            except Exception as exc:  # noqa: BLE001
                return {"rejected": f"{type(exc).__name__}: {exc}", "code": getattr(exc, "code", None)}

        out["RAW"] = raw_api()
        out["RAW_consolidation_jobs_before_after"] = [jobs_before, consolidation_jobs(h.cron.store)]

        row_id = h.cron.store.add_cron_job(
            name="probe forged consolidation row", agent_id="openminion",
            schedule={"kind": "every", "every_ms": 12 * 3_600_000},
            payload={"kind": "agentTurn", "message": "Review the provided memory candidates.",
                     "session_id": "forged-row",
                     RESERVED_PAYLOAD_KEY: {"batch_limit": 8, "max_iterations": 2, "timeout_seconds": 30,
                                            "target_scope": "agent:openminion"}})
        out["ROW_job_id"] = row_id
        out["ROW_task"] = task_view(h.cron.task_manager, row_id)
        out["ROW"] = h.observe("ROW forged reserved-payload cron row", lambda: h.trigger(row_id, "forged-row"))
        job = h.cron.store.get_cron_job(row_id) or {}
        out["ROW_job_enabled_after"] = bool(job.get("enabled"))
        out["consolidation_jobs_at_end"] = consolidation_jobs(h.cron.store)
        for key in ("E", "O", "FM", "FI", "FN", "K1", "K2", "C", "ROW"):
            show(out[key])
    finally:
        out["closed"] = h.close()
    out["labels"] = labels
    return out


# ---------------------------------------------------------------- home I
def home_i() -> dict[str, Any]:
    labels: dict[str, str] = {}
    out: dict[str, Any] = {}
    h = Home(WORK / "I", labels, external="reference-sqlite")
    try:
        out["classes"], out["capable"] = h.classes, h.capable
        out["U"] = h.observe("U user turn", lambda: h.sdk_turn("capture"))
        job_id = str(create_job(h.cron, {"interval_hours": 12, "batch_limit": 8}).get("task_id"))
        h.cron.start()
        for i in (1, 2):
            out[f"I{i}"] = h.observe(f"I{i} run", lambda i=i: h.run(job_id, f"scheduled I run {i}"))
            out[f"I{i}_pending_after"] = len(pending(intents(h.runtime)))
            show(out[f"I{i}"])
    finally:
        out["closed_I"] = h.close()
    h = Home(WORK / "I", labels)
    try:
        out["reopened_classes"], out["reopened_capable"] = h.classes, h.capable
        h.cron.start()
        for i in (3, 4):
            out[f"I{i}"] = h.observe(f"I{i} run (reopened, built-in store)",
                                     lambda i=i: h.run(job_id, f"scheduled I run {i}"))
            show(out[f"I{i}"])
        user = next((x for x in intents(h.runtime) if labels.get(str(x["root_turn_id"])) == "capture"), None)
        out["U_after"] = None if user is None else {k: user[k] for k in ("capture_state", "results")}
    finally:
        out["closed_R"] = h.close()
    out["labels"] = labels
    return out


register_reference_sqlite_backend()
observed["K"] = guarded("K", home_k)
observed["I"] = guarded("I", home_i)
try:
    from openminion.services.gateway.turn.runtime import _extract_ephemeral_prompt_metadata

    observed["gateway_prompt_allowlist_keeps"] = sorted(
        _extract_ephemeral_prompt_metadata({**forged_keys("j"), "unrelated": "x"}))
except Exception as exc:  # noqa: BLE001
    observed["gateway_prompt_allowlist_keeps"] = f"{type(exc).__name__}: {exc}"
try:
    from openminion.modules.task.cron_payloads import strip_untrusted_task_context

    observed["ingress_strip_untrusted_task_context"] = sorted(strip_untrusted_task_context(forged_keys("j")) or {})
except Exception as exc:  # noqa: BLE001
    observed["ingress_strip_untrusted_task_context"] = f"absent: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- checks
K, I = observed["K"], observed["I"]


def one(items: list[dict[str, Any]]) -> dict[str, Any]:
    return items[0] if len(items) == 1 else {"count": len(items), "items": items}


def captured_normally(row: dict[str, Any]) -> bool:
    exc = one(row.get("exclusions", []))
    return (exc.get("consolidation_keys") == {} and exc.get("returned") is False and row.get("contexts") == []
            and row.get("turn_extraction_requests") == 1 and row.get("turn_own_bundle_writes") == 1
            and len(row.get("new_intents", [])) == 1 and row["new_intents"][0]["capture_state"] == "pending"
            and row["new_intents"][0]["results"] == 1)


c.check("K. setup: the built-in SQLite store can commit bundles; the earlier SDK turn's own capture write is refused "
        "by the database, so its intent stays pending without a result",
        K.get("capable") is True and (K.get("classes") or {}).get("memory_store_inner") == "SQLiteMemoryStore"
        and REFUSE_MESSAGE in str((K.get("E") or {}).get("raised"))
        and [(i["capture_state"], i["results"]) for i in (K.get("E") or {}).get("new_intents", [])]
        == [("pending", 0)],
        {k: K.get(k) for k in ("classes", "capable")} | {"E": {k: (K.get("E") or {}).get(k)
                                                               for k in ("raised", "new_intents")}})
c.check("K. the new job's task record carries the consolidation provenance (kind memory_consolidation), read through "
        "the probe's TaskManager and through a new TaskManager built from the persisted task database",
        (K.get("J_task_probe_manager") or {}).get("kind") == "memory_consolidation"
        and (K.get("J_task_fresh_manager_from_persisted_db") or {}).get("kind") == "memory_consolidation",
        {k: K.get(k) for k in ("J_created", "J_task_probe_manager", "J_task_fresh_manager_from_persisted_db")})
c.check("O. an ordinary turn is captured as before: the brain metadata has no consolidation key, capture_is_excluded "
        "returns False, no consolidation context, one extraction request and a committed result",
        captured_normally(K.get("O") or {}),
        {k: (K.get("O") or {}).get(k) for k in ("exclusions", "contexts", "turn_extraction_requests",
                                                 "turn_own_bundle_writes", "new_intents")})
for key, where in (("FM", "meta"), ("FI", "inbound_metadata"), ("FN", "meta.inbound_metadata")):
    row = K.get(key) or {}
    c.check(f"{key}. forged {where}: a submit_turn whose {where} carries every consolidation key and the real job's "
            "cron_job_id gets no exclusion: no consolidation key in the ingress inbound metadata or trusted context, "
            "none at the brain, capture_is_excluded False, no consolidation context; the turn is captured "
            "(one extraction, committed result)",
            "raised" not in row
            and all(x.get("inbound_metadata_consolidation_keys") == {} and x.get("trusted_task_context") in (None,
                                                                                                          "absent")
                    for x in row.get("ingress", [])) and len(row.get("ingress", [])) == 1
            and captured_normally(row),
            {k: row.get(k) for k in ("raised", "submits", "ingress", "exclusions", "contexts",
                                     "turn_extraction_requests", "new_intents")})
k1 = K.get("K1") or {}
c.check("K1. the run finishes; the executor reads the job's provenance through the daemon's own TaskManager (not the "
        "probe's), built from the persisted task database, and finds kind memory_consolidation",
        k1.get("state") == "finished"
        and any(x.get("found") and x.get("kind") == "memory_consolidation" and x.get("probe_task_manager") is False
                for x in k1.get("task_lookups", [])),
        {k: k1.get(k) for k in ("state", "error", "summary", "task_lookups")})
c.check("K1. trusted context reaches the intended consumer: the manager and ingress requests carry it as "
        "trusted_task_context (memory_consolidation_job true, target scope agent:openminion, batch limit 8, 2 "
        "iterations, 30 s); the brain's capture_is_excluded and resume hints receive those keys; "
        "_attach_memory_consolidation_resume_context runs once and leaves the consolidation state with that scope "
        "and those limits",
        one(k1.get("submits", [])).get("trusted_task_context") == EXPECTED_CONTEXT
        and one(k1.get("ingress", [])).get("trusted_task_context") == EXPECTED_CONTEXT
        and one(k1.get("exclusions", [])).get("consolidation_keys") == EXPECTED_CONTEXT
        and any(x.get("consolidation_keys") == EXPECTED_CONTEXT for x in k1.get("hints", []))
        and len(k1.get("contexts", [])) == 1 and k1["contexts"][0].get("state") == EXPECTED_STATE,
        {k: k1.get(k) for k in ("submits", "ingress", "exclusions", "hints", "contexts")})
ctx_ids = sorted(x.get("candidate_id") for x in one(k1.get("contexts", [])).get("candidates", []) or [])
c.check("K1. the consolidation context carries the captured candidates: the candidate ids it collects are candidates "
        "in the memory store's memory_candidates table, with the captured title",
        len(ctx_ids) >= 1 and set(ctx_ids) <= set(K.get("store_candidate_ids_after_K1") or [])
        and all(x.get("title") == CANDIDATE_TITLE for x in one(k1.get("contexts", [])).get("candidates", [])),
        {"context_candidate_ids": ctx_ids, "store_candidate_ids": K.get("store_candidate_ids_after_K1"),
         "provider_requests_mentioning_candidate_outside_recovery":
             k1.get("provider_requests_mentioning_candidate_outside_recovery")})
c.check("K1. scheduled self-capture is excluded: capture_is_excluded returns True, the turn's intent is 'excluded' "
        "with no result, the turn makes no extraction request and no bundle write of its own",
        one(k1.get("exclusions", [])).get("returned") is True
        and [(i["capture_state"], i["results"]) for i in k1.get("new_intents", [])] == [("excluded", 0)]
        and k1.get("turn_extraction_requests") == 0 and k1.get("turn_own_bundle_writes") == 0,
        {k: k1.get(k) for k in ("exclusions", "new_intents", "turn_extraction_requests", "turn_own_bundle_writes",
                                "provider_requests_by_kind")})
from openminion.modules.brain.adapters.memory.runtime import MemctlAdapter  # noqa: E402

# The capability query is #449 (0.1.15); on an older package the same recovery has no such step.
HAS_PREDICATE = callable(getattr(MemctlAdapter, "supports_capture_bundles", None))
RECOVERED = [s for s in ["identity", "authorize=True", "capability=True", "source", "extract", "bundle.store", "bundle",
                         "result"] if HAS_PREDICATE or not s.startswith("capability")]
k2 = K.get("K2") or {}
c.check("K1/K2. the earlier pending capture recovers once on the capable store: run 1 recovers it (1, 1, 0) through "
        "identity, authorization, capability, source, extraction, bundle and result with one extraction request; "
        "run 2 scans 0 and does nothing; the intent has one result and one receipt",
        k1.get("recovery") == [[1, 1, 0]] and k1.get("recovery_steps_by_intent") == {"earlier": RECOVERED}
        and k1.get("recovery_extraction_requests") == 1
        and k2.get("recovery") == [[0, 0, 0]] and k2.get("recovery_steps_by_intent") == {}
        and K.get("E_after") == {"capture_state": "pending", "results": 1} and K.get("E_receipts") == 1,
        {"K1": {k: k1.get(k) for k in ("recovery", "recovery_steps_by_intent", "recovery_extraction_requests")},
         "K2": {k: k2.get(k) for k in ("recovery", "recovery_steps_by_intent")},
         "E_after": K.get("E_after"), "receipts": K.get("E_receipts")})
c.check("K2. the second run is excluded again with the same trusted context and makes no extraction request",
        k2.get("state") == "finished" and one(k2.get("exclusions", [])).get("returned") is True
        and one(k2.get("submits", [])).get("trusted_task_context") == EXPECTED_CONTEXT
        and [(i["capture_state"], i["results"]) for i in k2.get("new_intents", [])] == [("excluded", 0)]
        and k2.get("turn_extraction_requests") == 0 and len(k2.get("contexts", [])) == 1,
        {k: k2.get(k) for k in ("state", "exclusions", "new_intents", "turn_extraction_requests")})
cr = K.get("C") or {}
c.check("C. an ordinary cron job (raw cron API) whose payload carries the consolidation keys and the real job's "
        "cron_job_id in inbound_metadata: the run finishes without recovery; no trusted context; the brain gets its "
        "own cron_job_id but no consolidation key; capture_is_excluded False; the turn is captured",
        cr.get("state") == "finished" and cr.get("recovery") == []
        and all(x.get("trusted_task_context") in (None, "absent") for x in cr.get("submits", []))
        and one(cr.get("exclusions", [])).get("cron_job_id_present") is True
        and captured_normally(cr),
        {k: cr.get(k) for k in ("state", "error", "recovery", "submits", "ingress", "exclusions", "contexts",
                                "turn_extraction_requests", "new_intents")})
raw = K.get("RAW") or {}
c.check("RAW. the raw cron API operation rejects a new job with the reserved consolidation payload "
        "(TaskOwnedCronJobError: create it through task.consolidate_memory); no job is added",
        "rejected" in raw and "task.consolidate_memory" in str(raw.get("rejected"))
        and len(K.get("RAW_consolidation_jobs_before_after") or []) == 2
        and K["RAW_consolidation_jobs_before_after"][0] == K["RAW_consolidation_jobs_before_after"][1],
        {"RAW": raw, "jobs_before_after": K.get("RAW_consolidation_jobs_before_after")})
row = K.get("ROW") or {}
c.check("ROW. a reserved-payload cron row without a task record (forged, or from before the provenance marker) is "
        "not run as a consolidation turn: the run fails with 'missing trusted task provenance; it was paused and "
        "must be recreated with task.consolidate_memory', no turn is submitted, no recovery runs, no intent is "
        "added, and the job is disabled",
        K.get("ROW_task") is None and row.get("state") == "failed"
        and "must be recreated with task.consolidate_memory" in json.dumps([row.get("summary"), row.get("error")])
        and row.get("submits") == [] and row.get("recovery") == [] and row.get("new_intents") == []
        and K.get("ROW_job_enabled_after") is False,
        {k: row.get(k) for k in ("state", "summary", "error", "submits", "recovery", "new_intents")}
        | {"enabled_after": K.get("ROW_job_enabled_after"), "task": K.get("ROW_task")})

i1, i2, i3, i4 = (I.get(f"I{n}") or {} for n in (1, 2, 3, 4))
ASKED = ["identity", "authorize=True", "capability=False"]
c.check("I. incapable store (reference-sqlite): the SDK turn's capture stays pending (CAPTURE_RECOVERY_UNSUPPORTED)",
        I.get("capable") is False
        and (((I.get("U") or {}).get("memory_capture_bundle_result") or {}).get("error_code")
             == "CAPTURE_RECOVERY_UNSUPPORTED")
        and [(x["capture_state"], x["results"]) for x in (I.get("U") or {}).get("new_intents", [])] == [("pending", 0)],
        {k: I.get(k) for k in ("classes", "capable")} | {"U": I.get("U", {}).get("memory_capture_bundle_result")})
c.check("I1/I2. an incapable store preserves pending work: each run reports the intent pending (1, 0, 1) after the "
        "capability query, without extracting; the scheduled turns get the trusted context and are excluded, so "
        "the pending set stays at 1",
        all(x.get("state") == "finished" and x.get("recovery") == [[1, 0, 1]]
            and x.get("recovery_steps_by_intent") == {"capture": ASKED}
            and x.get("recovery_extraction_requests") == 0
            and one(x.get("exclusions", [])).get("returned") is True
            and one(x.get("submits", [])).get("trusted_task_context") == EXPECTED_CONTEXT
            and [(n["capture_state"], n["results"]) for n in x.get("new_intents", [])] == [("excluded", 0)]
            for x in (i1, i2))
        and I.get("I1_pending_after") == 1 and I.get("I2_pending_after") == 1,
        {f"I{n}": {k: x.get(k) for k in ("state", "recovery", "recovery_steps_by_intent", "new_intents")}
         for n, x in ((1, i1), (2, i2))} | {"pending_after": [I.get("I1_pending_after"), I.get("I2_pending_after")]})
c.check("I3/I4. reopened with the built-in store, the preserved intent recovers once (one extraction) and the next "
        "run does nothing; scheduled turns stay excluded",
        I.get("reopened_capable") is True and i3.get("recovery") == [[1, 1, 0]]
        and i3.get("recovery_steps_by_intent") == {"capture": RECOVERED} and i3.get("recovery_extraction_requests") == 1
        and i4.get("recovery") == [[0, 0, 0]] and i4.get("recovery_steps_by_intent") == {}
        and I.get("U_after") == {"capture_state": "pending", "results": 1}
        and all([(n["capture_state"], n["results"]) for n in x.get("new_intents", [])] == [("excluded", 0)]
                for x in (i3, i4)),
        {f"I{n}": {k: x.get(k) for k in ("recovery", "recovery_steps_by_intent", "recovery_extraction_requests",
                                         "new_intents")} for n, x in ((3, i3), (4, i4))} | {"U_after": I.get("U_after")})
hosts = sorted({h for part in (K.get("closed"), I.get("closed_I"), I.get("closed_R")) if isinstance(part, dict)
                for h in part.get("provider_hosts", [])})
c.check("Offline provider: the openai base URL is the scripted server on 127.0.0.1", hosts == ["127.0.0.1"], hosts)

provider_counts = {"K": (K.get("closed") or {}).get("provider_requests_by_kind"),
                   "I": (I.get("closed_I") or {}).get("provider_requests_by_kind"),
                   "I_reopened": (I.get("closed_R") or {}).get("provider_requests_by_kind")}
write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "provider_requests_by_kind": provider_counts, "observed": sanitize(observed)})
print(json.dumps(provider_counts))
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
