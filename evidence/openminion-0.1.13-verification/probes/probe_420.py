"""#420 on the installed package: memory-consolidation cron runs with a pending terminal capture intent.

Each case builds a real `APIRuntime` (one agent, `openminion`; the SDK's
`openai` provider pointed at a scripted OpenAI-compatible server on 127.0.0.1,
no external network) and runs one SDK turn, "Please remember that I prefer
tabs over spaces for indentation.", in session `capture`. The turn's auto fact
extraction returns one user_preference candidate. Then the memory
consolidation job runs through the package's own scheduling path:

  - the job is created by the installed `task.consolidate_memory` tool handler
    (`_h_task_consolidate_memory`) with the daemon's TaskManager over the
    daemon's cron store (the runtime's brain `sessions.db`);
  - `TaskManager.run_task_now` (the `task.run_now` path) queues a run;
  - the daemon's `attach_cron_scheduler(runtime=...)` starts the real
    `CronScheduler` with the daemon's `CronTurnExecutor` on this runtime; the
    scheduler claims the run, the executor recovers pending captures and
    submits the scheduled turn to the runtime's AgentRuntimeManager, and the
    scheduler persists the run's outcome.

Each case runs the job twice (two queued runs). Observers wrap the runtime's own
objects and call through (probe_420_lib.Observers): recover_pending_captures,
the agent service's capture candidate extractor, the memctl bundle write, the
security policy's evaluate, submit_turn and the scheduler's cron events.

  S  Recovery: the turn's capture bundle write fails once with StoreWriteError
     (injected, as upstream's #420 regression test does), leaving its terminal
     intent pending. Run 1 recovers it once through the brain session API with
     the same capture identity, writes the bundle (candidate + receipt) and
     then submits the scheduled turn; run 2 finds nothing pending.
  D  Authorization denial: the agent's identity profile (profile.yaml under the
     configured identity bundle root) has the hard constraint "NEVER write
     memory records". Same pending intent; recovery is denied: no extraction,
     no write, the intent stays pending; the scheduled turn still runs.
  U  Unsupported capture: the bundled `reference-sqlite` external backend (no
     atomic capture). The turn's intent is pending (CAPTURE_RECOVERY_UNSUPPORTED);
     recovery extracts, the write raises CaptureRecoveryUnsupportedError, the
     intent stays pending; the scheduled turn still runs.
  N  No pending intent (control): the turn's own capture succeeds; recovery
     scans nothing, calls no extractor; the scheduled turn runs.
  P  Only when OPENMINION_TEST_POSTGRES_URL is set (the Linux runner): case S
     with the built-in memory provider's store on PostgreSQL (its own schema,
     registered memory config); the bundle and receipt are read server-side.

Not production behaviour, said plainly: the one capture write failure in S, D
and P is injected by the probe (the turn's bundle write raises StoreWriteError
once), the job is created by calling the tool's handler directly, and the
recovery-denial constraint comes from an identity profile the probe writes.

Usage: python -I -X utf8 probe_420.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import os
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


_pg_url = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()
_extra: dict[str, Any] = {}
if _pg_url:
    import sqlalchemy as _sa

    _engine = _sa.create_engine(_pg_url, future=True)
    with _engine.connect() as _conn:
        _extra["postgres_version"] = _conn.execute(_sa.text("SELECT version()")).scalar_one()
    _engine.dispose()
else:
    _extra["postgres"] = "not run: OPENMINION_TEST_POSTGRES_URL is not set"
info = banner("probe_420", provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "SQLAlchemy", "psycopg", "httpx",
                                                   "pydantic")}, **_extra)

from openminion import Agent  # noqa: E402
from openminion.modules.memory.backends.external import register_reference_sqlite_backend  # noqa: E402
from openminion.modules.session.capture import build_capture_identity  # noqa: E402
from probe_420_lib import (  # noqa: E402
    AGENT_ID,
    CAPTURE_ITEM,
    CAPTURE_MESSAGE,
    Cron,
    Observers,
    ScriptedProvider,
    brain_events,
    build_runtime,
    chdir_env,
    intent_view,
    restore_env,
    session_events,
    sqlite_rows,
)

c = Checks()
observed: dict[str, Any] = {}
DENY_CONSTRAINT = "NEVER write memory records"
CAPTURE_SESSION = "capture"
PG_BASE = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()


class PgSchema:
    """One PostgreSQL schema for the P case's memory store; rows are read server-side (URL never printed)."""

    def __init__(self, label: str) -> None:
        import uuid
        from urllib.parse import quote_plus

        import sqlalchemy as sa

        self.sa = sa
        self.admin = sa.create_engine(PG_BASE, future=True)
        self.schema = f"probe420_{label}_{uuid.uuid4().hex[:10]}"
        with self.admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{self.schema}"'))
        sep = "&" if "?" in PG_BASE else "?"
        self.url = f"{PG_BASE}{sep}options={quote_plus('-csearch_path=' + self.schema)}"

    def memory_config(self, **kwargs: Any) -> Any:
        import dataclasses

        from openminion.modules.memory.config import from_base_config

        memory = from_base_config(**kwargs)
        return dataclasses.replace(memory, store=dataclasses.replace(memory.store, backend="postgres",
                                                                      postgres_url=self.url))

    def rows(self, sql: str, params: dict[str, Any]) -> list[tuple] | str:
        try:
            engine = self.sa.create_engine(self.url, future=True)
            try:
                with engine.connect() as conn:
                    return [tuple(r) for r in conn.execute(self.sa.text(sql), params)]
            finally:
                engine.dispose()
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"

    def memory_rows(self, capture_id: str) -> dict[str, Any]:
        meta = "CAST(meta_json AS jsonb)"
        return {
            "receipts": self.rows("SELECT capture_id, disposition, result_hash, report_hash, output_ids_json::text "
                                  "FROM memory_capture_bundle_receipts WHERE capture_id = :c", {"c": capture_id}),
            "candidates": self.rows(f"SELECT candidate_id, session_id, type, key, {meta}->>'capture_id', "
                                    f"{meta}->>'source_root_turn_id', {meta}->>'source_session_id' "
                                    f"FROM memory_candidates WHERE {meta}->>'capture_id' = :c", {"c": capture_id}),
        }

    def tables(self) -> list[str] | str:
        rows = self.rows("SELECT table_name FROM information_schema.tables WHERE table_schema = :s ORDER BY 1",
                         {"s": self.schema})
        return [r[0] for r in rows] if isinstance(rows, list) else rows

    def drop(self) -> None:
        with self.admin.begin() as conn:
            conn.execute(self.sa.text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
        self.admin.dispose()


def parse(value: Any) -> dict[str, Any]:
    try:
        return json.loads(value) if isinstance(value, str) else dict(value or {})
    except (TypeError, ValueError):
        return {"unparsed": value}


def results_for(runtime: Any, capture_id: str) -> list[dict[str, Any]]:
    return [dict(e.payload) | {"canonical_event_id": e.canonical_event_id}
            for e in session_events(runtime.sessions, "memory.capture.result")
            if e.payload.get("capture_id") == capture_id]


def memory_rows(store_db: Any, capture_id: str) -> dict[str, Any]:
    if isinstance(store_db, PgSchema):
        return store_db.memory_rows(capture_id)
    if store_db is None:
        return {"store": "not a built-in SQLite memory store"}
    return {
        "receipts": sqlite_rows(store_db, "SELECT capture_id, disposition, result_hash, report_hash, output_ids_json "
                                          "FROM memory_capture_bundle_receipts WHERE capture_id = ?", (capture_id,)),
        "candidates": sqlite_rows(store_db, "SELECT candidate_id, session_id, type, key, "
                                            "json_extract(meta_json, '$.capture_id'), "
                                            "json_extract(meta_json, '$.source_root_turn_id'), "
                                            "json_extract(meta_json, '$.source_session_id') FROM memory_candidates "
                                            "WHERE json_extract(meta_json, '$.capture_id') = ?", (capture_id,)),
    }


def run_case(name: str, *, external: str | None = None, identity_constraint: str | None = None,
             fail_turn_capture: bool = True, runs: int = 2, pg: "PgSchema | None" = None) -> dict[str, Any]:
    root = WORK / name
    provider = ScriptedProvider()
    old_env = chdir_env(root)
    row: dict[str, Any] = {}
    runtime = None
    cron = None
    obs = None
    try:
        runtime = build_runtime(root, provider, external_adapter=external, identity_constraint=identity_constraint,
                                memory_config=pg.memory_config if pg is not None else None)
        obs = Observers(runtime).install()
        store = obs.assembly.service._store
        inner = getattr(store, "_store", None)
        store_db: Any = Path(str(inner.db_path)) if inner is not None and getattr(inner, "db_path", None) else None
        if pg is not None:
            store_db = pg
        row["classes"] = {
            "runtime_session_store": type(runtime.sessions).__name__,
            "brain_session_api": type(obs.brain_session_api).__name__,
            "brain_session_api_store": type(getattr(obs.brain_session_api, "store", None)).__name__,
            "memory_store": type(store).__name__,
            "memory_store_inner": type(inner).__name__ if inner is not None else None,
            "memory_backend": type(getattr(store, "_backend", None)).__name__ if hasattr(store, "_backend") else None,
            "memctl": type(obs.memctl).__name__,
            "agent_service": type(obs.service).__name__,
            "runtime_manager": type(runtime.runtime_manager).__name__,
            "security_policy": type(obs.security_policy).__name__,
        }
        row["identity_constraints_loaded"] = list(getattr(obs.security_policy, "_identity_constraints", []) or [])
        # ------------------------------------------------ the SDK turn that leaves the intent
        obs.fail_next_capture_writes = 1 if fail_turn_capture else 0
        with Agent(runtime=runtime) as agent:
            try:
                res = agent.run(CAPTURE_MESSAGE, session_id=CAPTURE_SESSION, timeout_seconds=120)
                md = dict(res.metadata or {})
                row["turn"] = {"run_state": res.run_state, "text": res.text,
                               "memory_capture_state": md.get("memory_capture_state"),
                               "memory_capture_bundle_result": parse(md.get("memory_capture_bundle_result"))}
            except Exception as exc:  # noqa: BLE001
                row["turn"] = {"raised": f"{type(exc).__name__}: {exc}"}
        row["turn_bundle_writes"] = list(obs.bundle_writes)
        intents = [e for e in session_events(runtime.sessions, "turn.outcome", CAPTURE_SESSION)]
        row["intents_before"] = [intent_view(e) for e in intents]
        intent = row["intents_before"][0] if intents else {}
        capture_id = str(intent.get("capture_id") or "")
        root_turn = str(intent.get("root_turn_id") or "")
        identity = build_capture_identity(runtime_session_id=CAPTURE_SESSION, root_turn_id=root_turn) if root_turn else None
        row["canonical_identity"] = ({"capture_id": identity.capture_id, "event_id": identity.event_id}
                                     if identity is not None else None)
        row["results_before"] = results_for(runtime, capture_id)
        row["memory_rows_before"] = memory_rows(store_db, capture_id)
        row["brain_extraction_events_before"] = brain_events(obs.brain_session_api, CAPTURE_SESSION,
                                                             "brain.auto_fact_extraction", root_turn)
        # ------------------------------------------------ the consolidation job through the scheduler
        cron = Cron(runtime, root)
        created = cron.create_consolidation_job()
        job_id = str(created.get("task_id"))
        job = cron.store.get_cron_job(job_id) or {}
        row["job"] = {"created": {k: created.get(k) for k in ("ok", "task_id", "name", "enabled", "schedule",
                                                               "batch_limit", "target_scope",
                                                               "consolidation_session_id")},
                      "payload": job.get("payload"), "session_target": job.get("session_target"),
                      "concurrency_key": job.get("concurrency_key"), "max_attempts": job.get("max_attempts")}
        cron_session = str((job.get("payload") or {}).get("session_id") or "")
        scheduler = cron.start()
        row["scheduler"] = type(scheduler).__name__
        row["runs"] = []
        for index in range(runs):
            marks = {k: len(getattr(obs, k)) for k in ("recoveries", "extractor_calls", "bundle_writes",
                                                       "submitted_turns", "policy_decisions", "timeline")}
            provider_before = len(provider.requests)
            run: dict[str, Any] = {}
            try:
                queued = cron.run_now(job_id)
                run["queued"] = {k: queued.get(k) for k in ("run_id", "job_id", "state")}
                done = cron.wait(job_id, str(queued.get("run_id")))
                # The scheduler persists the run's outcome, then emits cron.run.finished.
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not any(
                        e.get("event") == "cron.run.finished" and e.get("run_id") == queued.get("run_id")
                        for e in obs.cron_events):
                    time.sleep(0.05)
            except Exception as exc:  # noqa: BLE001
                run["queue_error"] = f"{type(exc).__name__}: {exc}"
                done = {}
            output = done.get("output") or {}
            run["row"] = {"state": done.get("state"), "summary": done.get("summary"),
                          "isolated_session_id": done.get("isolated_session_id"), "error": done.get("error"),
                          "attempts": done.get("attempts"),
                          "watermark_target_scope": (output.get("coordination_watermark") or {}).get("target_scope"),
                          "probe_wait": done.get("probe_wait")}
            run_id = run.get("queued", {}).get("run_id")
            run["cron_events"] = [e for e in obs.cron_events if e.get("run_id") == run_id]
            run["recoveries"] = [{k: v for k, v in r.items() if not k.startswith("t_")}
                                 for r in obs.recoveries[marks["recoveries"]:]]
            windows = [(r["t_start"], r["t_end"]) for r in obs.recoveries[marks["recoveries"]:]]
            run["provider_requests_during_recovery"] = [k for s, e in windows for k in provider.kinds_between(s, e)]
            run["provider_requests"] = [r["kind"] for r in provider.requests[provider_before:]]
            run["extractor_calls"] = obs.extractor_calls[marks["extractor_calls"]:]
            run["bundle_writes"] = obs.bundle_writes[marks["bundle_writes"]:]
            run["policy_decisions"] = obs.policy_decisions[marks["policy_decisions"]:]
            run["submitted_turns"] = obs.submitted_turns[marks["submitted_turns"]:]
            run["timeline"] = [t["what"] for t in obs.timeline[marks["timeline"]:]]
            run["results_for_intent"] = results_for(runtime, capture_id)
            run["cron_session_outcomes"] = [intent_view(e) for e in
                                            session_events(runtime.sessions, "turn.outcome", cron_session)]
            run["memory_rows"] = memory_rows(store_db, capture_id)
            run["brain_extraction_events"] = brain_events(obs.brain_session_api, CAPTURE_SESSION,
                                                          "brain.auto_fact_extraction", root_turn)
            run["runtime_store_extraction_events"] = len(session_events(runtime.sessions,
                                                                        "brain.auto_fact_extraction*"))
            row["runs"].append(run)
        row["intents_after"] = [intent_view(e) for e in session_events(runtime.sessions, "turn.outcome",
                                                                       CAPTURE_SESSION)]
        row["provider_requests_total"] = len(provider.requests)
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
        row["scheduler_events"] = [e for e in obs.cron_events if not e.get("run_id")]
        row["capture_id"] = capture_id
        row["root_turn_id"] = root_turn
        row["cron_session"] = cron_session
        if pg is not None:
            row["postgres_schema_tables"] = pg.tables()
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


# ---------------------------------------------------------------- helpers for checks
def setup_pending(row: dict[str, Any], *, unsupported: bool = False) -> bool:
    intents = row.get("intents_before", [])
    turn = row.get("turn", {})
    ident = row.get("canonical_identity") or {}
    ok = (turn.get("run_state") == "completed" and len(intents) == 1 and intents[0]["capture_state"] == "pending"
          and intents[0]["capture_id"] == ident.get("capture_id")
          and intents[0]["canonical_event_id"] == ident.get("event_id")
          and row.get("results_before") == [])
    writes = row.get("turn_bundle_writes", [])
    if unsupported:
        ok = ok and (turn.get("memory_capture_bundle_result", {}).get("error_code") == "CAPTURE_RECOVERY_UNSUPPORTED"
                     and any("CaptureRecoveryUnsupportedError" in str(w.get("raised")) for w in writes))
    else:
        ok = ok and [w.get("injected") for w in writes] == ["StoreWriteError"]
    return ok


def job_ok(row: dict[str, Any]) -> bool:
    payload = (row.get("job") or {}).get("payload") or {}
    cons = payload.get("_openminion_memory_consolidation") or {}
    return (payload.get("kind") == "agentTurn" and cons.get("target_scope") == f"agent:{AGENT_ID}"
            and row.get("scheduler") == "CronScheduler")


def turn_ran(run: dict[str, Any], expected_outcomes: int) -> bool:
    r = run.get("row", {})
    events = [e["event"] for e in run.get("cron_events", [])]
    tl = run.get("timeline", [])
    after_recovery = ("recovery.end" in tl and "submit_turn" in tl
                      and tl.index("recovery.end") < tl.index("submit_turn"))
    submitted = run.get("submitted_turns", [])
    return (r.get("state") == "finished" and r.get("error") is None and len(submitted) == 1
            and submitted[0].get("session_id") == r.get("isolated_session_id") and submitted[0].get("agent_id") == AGENT_ID
            and str(submitted[0].get("input_text") or "").startswith("Review the provided memory candidates")
            and after_recovery and "cron.run.started" in events and "cron.run.finished" in events
            and r.get("watermark_target_scope") == f"agent:{AGENT_ID}"
            and len(run.get("cron_session_outcomes", [])) == expected_outcomes
            and r.get("summary") == "openminion: Consolidation reviewed; nothing to change.")


def extractor_by_keyword(call: dict[str, Any], row: dict[str, Any]) -> bool:
    return (call.get("positional_args") == 0
            and call.get("keywords") == ["root_turn_id", "session_api", "session_id", "user_message"]
            and call.get("session_api_is_brain_runner_session_api") is True
            and call.get("session_api_is_runtime_session_store") is False
            and call.get("session_id") == CAPTURE_SESSION and call.get("root_turn_id") == row.get("root_turn_id")
            and call.get("user_message") == CAPTURE_MESSAGE and "raised" not in call)


def run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {"row": run.get("row"), "recoveries": run.get("recoveries"), "timeline": run.get("timeline"),
            "submitted_turns": run.get("submitted_turns"), "extractor_calls": len(run.get("extractor_calls", []))}


def capture_writes(run: dict[str, Any], capture_id: str) -> list[dict[str, Any]]:
    return [w for w in run.get("bundle_writes", []) if w.get("capture_id") == capture_id]


# ---------------------------------------------------------------- recovery checks (S, and P on PostgreSQL)
def recovery_checks(tag: str, s: dict[str, Any]) -> None:
    s1, s2 = ((s.get("runs") or []) + [{}, {}])[:2]
    cid = s.get("capture_id", "")
    c.check(f"{tag}. setup: the SDK turn completes; its capture bundle write fails once (injected StoreWriteError), leaving one "
            "pending terminal capture intent (turn.outcome, canonical capture identity) and no memory.capture.result",
            setup_pending(s), {"turn": s.get("turn"), "intents": s.get("intents_before"),
                               "canonical_identity": s.get("canonical_identity"), "results": s.get("results_before"),
                               "turn_bundle_writes": [w.get("injected") or w.get("raised") for w in
                                                      s.get("turn_bundle_writes", [])]})
    c.check(f"{tag}. the job comes from the installed task.consolidate_memory handler (agentTurn, consolidation target "
            "agent:openminion) and runs through the daemon's attach_cron_scheduler CronScheduler (run_task_now, claimed, "
            "cron.run.started, then the outcome persisted and cron.run.finished)",
            job_ok(s) and [e["event"] for e in s1.get("cron_events", [])][:1] == ["cron.run.started"]
            and "cron.run.finished" in [e["event"] for e in s1.get("cron_events", [])]
            and s1.get("row", {}).get("state") in ("finished", "failed"),
            {"job": s.get("job"), "scheduler": s.get("scheduler"), "run1": s1.get("row"), "events": s1.get("cron_events")})
    rec1 = s1.get("recoveries", [])
    c.check(f"{tag}. run 1: recovery gets the runtime session store as `sessions` and the cron adapter as extractor (not the "
            "service method); it recovers the intent once: scanned 1, recovered 1, pending 0",
            len(rec1) == 1 and rec1[0].get("result") == [1, 1, 0] and rec1[0].get("sessions_is_runtime_session_store")
            and not rec1[0].get("extract_candidates_is_service_method"), rec1)
    ex1 = s1.get("extractor_calls", [])
    c.check(f"{tag}. run 1: the agent's extractor is called once, by keyword only, with the brain runner's session API "
            "(SessctlAdapter, the runner's own object, not the runtime SessionStore), for the intent's session, root turn "
            "and original user message; it returns the extracted candidate",
            len(ex1) == 1 and extractor_by_keyword(ex1[0], s)
            and [{k: i.get(k) for k in ("kind", "normalized_key", "content")} for i in ex1[0].get("items", [])]
            == [{k: CAPTURE_ITEM[k] for k in ("kind", "normalized_key", "content")}], ex1)
    c.check(f"{tag}. run 1: the extraction's events go through the brain session API (brain.auto_fact_extraction.started and "
            ".completed with 1 item, trace = the root turn; none before the run), none in the runtime session store; the "
            "provider gets exactly one extraction request during recovery",
            s.get("brain_extraction_events_before") == []
            and [e["event_type"] for e in s1.get("brain_extraction_events", [])]
            == ["brain.auto_fact_extraction.started", "brain.auto_fact_extraction.completed"]
            and s1["brain_extraction_events"][1].get("extracted_items") == 1
            and all(e.get("trace_id") == s.get("root_turn_id") for e in s1["brain_extraction_events"])
            and s1.get("runtime_store_extraction_events") == 0
            and s1.get("provider_requests_during_recovery") == ["extraction"],
            {"before": s.get("brain_extraction_events_before"), "after_run1": s1.get("brain_extraction_events"),
             "runtime_store": s1.get("runtime_store_extraction_events"),
             "provider_during_recovery": s1.get("provider_requests_during_recovery")})
    w1 = capture_writes(s1, cid)
    res1 = s1.get("results_for_intent", [])
    ident = s.get("canonical_identity") or {}
    c.check(f"{tag}. run 1: capture identity is preserved: the recovery's bundle write carries the intent's capture_id, root "
            "turn, session and agent; the memory.capture.result has the same capture_id, root turn and runtime session, "
            "canonical id memory.capture.result:<capture_id>; the capture_id is the canonical one for (session, root turn)",
            len(w1) == 1 and {k: w1[0].get(k) for k in ("capture_id", "root_turn_id", "session_id", "agent_id")}
            == {"capture_id": cid, "root_turn_id": s.get("root_turn_id"), "session_id": CAPTURE_SESSION, "agent_id": AGENT_ID}
            and len(res1) == 1 and res1[0].get("capture_id") == cid == ident.get("capture_id")
            and res1[0].get("root_turn_id") == s.get("root_turn_id") and res1[0].get("runtime_session_id") == CAPTURE_SESSION
            and res1[0].get("canonical_event_id") == f"memory.capture.result:{cid}"
            and s.get("intents_after") == s.get("intents_before"),
            {"write": w1, "result": res1, "canonical_identity": ident, "intent_after": s.get("intents_after")})
    mr1 = s1.get("memory_rows", {})
    receipt = (w1[0].get("result") if w1 else {}) or {}
    c.check(f"{tag}. run 1: the bundle is written once with the extracted candidate: receipt disposition succeeded with one "
            "output id; the memory store holds that candidate (capture_id, source root turn and session of the intent) "
            "and one bundle receipt row with the result's hashes",
            len(w1) == 1 and len(w1[0].get("candidates", [])) == 1 and receipt.get("disposition") == "succeeded"
            and len(receipt.get("output_ids", [])) == 1 and len(res1) == 1
            and res1[0].get("result_hash") == receipt.get("result_hash")
            and isinstance(mr1.get("receipts"), list) and len(mr1["receipts"]) == 1
            and mr1["receipts"][0][2] == receipt.get("result_hash") and mr1["receipts"][0][3] == receipt.get("report_hash")
            and isinstance(mr1.get("candidates"), list) and len(mr1["candidates"]) == 1
            and mr1["candidates"][0][0] == receipt.get("output_ids", [None])[0]
            and list(mr1["candidates"][0][4:]) == [cid, s.get("root_turn_id"), CAPTURE_SESSION],
            {"receipt": receipt, "result": res1, "memory_rows": mr1})
    c.check(f"{tag}. run 1: after recovery the scheduled turn is submitted once (consolidation session) and completes; the run "
            "finishes with the turn's text and the consolidation watermark",
            turn_ran(s1, 1), run_summary(s1))
    c.check(f"{tag}. run 2: nothing pending (scanned 0), no extractor call, no new bundle write for the capture; still exactly "
            "one memory.capture.result, one receipt and one candidate for it; the second scheduled turn runs",
            [r.get("result") for r in s2.get("recoveries", [])] == [[0, 0, 0]] and s2.get("extractor_calls") == []
            and capture_writes(s2, cid) == [] and s2.get("results_for_intent") == res1
            and s2.get("memory_rows") == mr1 and len(s2.get("brain_extraction_events", [])) == 2 and turn_ran(s2, 2),
            {"recoveries": s2.get("recoveries"), "results": len(s2.get("results_for_intent", [])),
             "memory_rows": s2.get("memory_rows"), "run": s2.get("row")})


s = run_case("S")
observed["S_recovery"] = s
recovery_checks("S", s)

# ---------------------------------------------------------------- D. authorization denial
d = run_case("D", identity_constraint=DENY_CONSTRAINT)
observed["D_authorization_denied"] = d
d1, d2 = ((d.get("runs") or []) + [{}, {}])[:2]
did = d.get("capture_id", "")
c.check("D. setup: the identity profile's hard constraint is loaded into the agent's security policy; the turn leaves "
        "a pending intent as in S", d.get("identity_constraints_loaded") == [DENY_CONSTRAINT] and setup_pending(d),
        {"constraints": d.get("identity_constraints_loaded"), "turn": d.get("turn"), "intents": d.get("intents_before")})
pd = d1.get("policy_decisions", [])
c.check("D. run 1: the recovery authorization (memory.write, channel cron, target memory-capture-recovery) is not "
        "allowed (identity_hard_constraint_restricted); recovery leaves the intent pending: scanned 1, recovered 0, "
        "pending 1",
        len(pd) == 1 and pd[0].get("tool_name") == "memory.write" and pd[0].get("channel") == "cron"
        and not pd[0].get("allowed") and pd[0].get("reason_code") == "identity_hard_constraint_restricted"
        and pd[0].get("session_id") == CAPTURE_SESSION and pd[0].get("run_id") == d.get("root_turn_id")
        and [r.get("result") for r in d1.get("recoveries", [])] == [[1, 0, 1]],
        {"policy": pd, "recoveries": d1.get("recoveries")})
c.check("D. run 1: no extraction: the extractor is not called, the provider gets no request during recovery, and no "
        "brain.auto_fact_extraction event exists for the root turn",
        d1.get("extractor_calls") == [] and d1.get("provider_requests_during_recovery") == []
        and d1.get("brain_extraction_events") == [] and d1.get("runtime_store_extraction_events") == 0,
        {"extractor_calls": d1.get("extractor_calls"), "provider": d1.get("provider_requests_during_recovery"),
         "events": d1.get("brain_extraction_events")})
c.check("D. no write: no bundle write for the capture, no memory.capture.result, no candidate or receipt for it in the "
        "memory store, in either run; the intent stays pending (run 2 scans it again and leaves it pending)",
        capture_writes(d1, did) == [] and capture_writes(d2, did) == [] and d1.get("results_for_intent") == []
        and d2.get("results_for_intent") == [] and d2.get("memory_rows") == {"receipts": [], "candidates": []}
        and [r.get("result") for r in d2.get("recoveries", [])] == [[1, 0, 1]]
        and len(d2.get("policy_decisions", [])) == 1 and not d2["policy_decisions"][0].get("allowed")
        and d2.get("extractor_calls") == [] and d.get("intents_after") == d.get("intents_before"),
        {"writes": capture_writes(d1, did) + capture_writes(d2, did), "results": d2.get("results_for_intent"),
         "memory_rows": d2.get("memory_rows"), "run2_recoveries": d2.get("recoveries")})
c.check("D. the scheduled turn still runs after the denied recovery, in both runs", turn_ran(d1, 1) and turn_ran(d2, 2),
        {"run1": run_summary(d1), "run2": run_summary(d2)})

# ---------------------------------------------------------------- U. unsupported capture
register_reference_sqlite_backend()
u = run_case("U", external="reference-sqlite", fail_turn_capture=False)
observed["U_unsupported_capture"] = u
u1, u2 = ((u.get("runs") or []) + [{}, {}])[:2]
uid = u.get("capture_id", "")
c.check("U. setup: with the bundled reference-sqlite external backend (no atomic capture) the turn completes and its "
        "intent stays pending with CAPTURE_RECOVERY_UNSUPPORTED",
        setup_pending(u, unsupported=True) and u.get("classes", {}).get("memory_backend") == "SophiaGraphSqliteStore",
        {"classes": u.get("classes"), "turn": u.get("turn"), "intents": u.get("intents_before")})
uw1 = capture_writes(u1, uid)
ux1 = u1.get("extractor_calls", [])
c.check("U. run 1: recovery calls the extractor once by keyword with the brain session API for the intent; the bundle "
        "write for the same capture raises CaptureRecoveryUnsupportedError; scanned 1, recovered 0, pending 1",
        len(ux1) == 1 and extractor_by_keyword(ux1[0], u) and len(uw1) == 1
        and "CaptureRecoveryUnsupportedError" in str(uw1[0].get("raised"))
        and uw1[0].get("root_turn_id") == u.get("root_turn_id")
        and [r.get("result") for r in u1.get("recoveries", [])] == [[1, 0, 1]],
        {"extractor": ux1, "write": uw1, "recoveries": u1.get("recoveries")})
c.check("U. the capture remains pending: no memory.capture.result after either run; run 2 scans it again with the "
        "first scheduled turn's own unsupported intent (scanned 2, recovered 0, pending 2)",
        u1.get("results_for_intent") == [] and u2.get("results_for_intent") == []
        and [r.get("result") for r in u2.get("recoveries", [])] == [[2, 0, 2]]
        and u.get("intents_after") == u.get("intents_before"),
        {"run2_recoveries": u2.get("recoveries"), "results": u2.get("results_for_intent"),
         "run1_cron_session_outcomes": u1.get("cron_session_outcomes")})
c.check("U. the scheduled turn still runs after the unsupported recovery, in both runs",
        turn_ran(u1, 1) and turn_ran(u2, 2), {"run1": run_summary(u1), "run2": run_summary(u2)})

# ---------------------------------------------------------------- N. no pending intent
n = run_case("N", fail_turn_capture=False)
observed["N_no_pending"] = n
n1, n2 = ((n.get("runs") or []) + [{}, {}])[:2]
nid = n.get("capture_id", "")
nres = n1.get("results_for_intent", [])
c.check("N. setup (control): the turn's own capture succeeds: its intent has one memory.capture.result before any run",
        n.get("turn", {}).get("run_state") == "completed" and len(n.get("results_before", [])) == 1
        and n["results_before"][0].get("capture_id") == nid,
        {"turn": n.get("turn"), "results_before": n.get("results_before")})
c.check("N. both runs: recovery scans nothing (0, 0, 0), no extractor call, no bundle write for the capture, the result "
        "is unchanged",
        [r.get("result") for r in n1.get("recoveries", [])] == [[0, 0, 0]]
        and [r.get("result") for r in n2.get("recoveries", [])] == [[0, 0, 0]]
        and n1.get("extractor_calls") == [] and n2.get("extractor_calls") == []
        and capture_writes(n1, nid) == [] and capture_writes(n2, nid) == []
        and nres == n.get("results_before") and n2.get("results_for_intent") == nres,
        {"run1": n1.get("recoveries"), "run2": n2.get("recoveries"), "result": nres})
c.check("N. the scheduled turn runs in both runs", turn_ran(n1, 1) and turn_ran(n2, 2),
        {"run1": run_summary(n1), "run2": run_summary(n2)})

# ---------------------------------------------------------------- P. PostgreSQL memory store (runner only)
cases = [("S", s), ("D", d), ("U", u), ("N", n)]
if PG_BASE:
    pg = PgSchema("p")
    try:
        p_row = run_case("P", pg=pg)
    finally:
        try:
            pg.drop()
        except Exception as exc:  # noqa: BLE001
            observed["P_schema_drop"] = f"{type(exc).__name__}: {exc}"
    observed["P_postgres_memory_store"] = p_row
    tables = p_row.get("postgres_schema_tables")
    c.check("P. the S scenario with the built-in memory provider on a PostgreSQL memory store: the store is "
            "PostgresMemoryStore in the case's own schema, whose capture tables exist server-side",
            (p_row.get("classes") or {}).get("memory_store_inner") == "PostgresMemoryStore" and isinstance(tables, list)
            and {"memory_candidates", "memory_capture_bundle_receipts"} <= set(tables),
            {"classes": p_row.get("classes"), "schema_tables": tables})
    recovery_checks("P", p_row)
    cases.append(("P", p_row))
else:
    observed["P_postgres_memory_store"] = "not run: OPENMINION_TEST_POSTGRES_URL is not set"

# ---------------------------------------------------------------- offline provider
hosts = sorted({h for _, row in cases for h in row.get("provider_hosts", [])})
served = {k: r.get("provider_requests_total") for k, r in cases}
c.check("Offline provider: each case's openai provider base URL is the scripted server on 127.0.0.1, which answered "
        "every model request of the turns, extractions and scheduled turns",
        hosts == ["127.0.0.1"] and all(isinstance(v, int) and v > 0 for v in served.values()),
        {"hosts": hosts, "requests_served": served})

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": observed})
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
