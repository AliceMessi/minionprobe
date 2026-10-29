"""#449 on the installed package: pending-capture recovery asks the memory store before it extracts.

Installed package only (no repo test fixtures). Every case builds a real
`APIRuntime` (one agent, `openminion`; the SDK's `openai` provider pointed at a
scripted OpenAI-compatible server on 127.0.0.1, no external network; the
harness of the #449 report, probe_449_lib.py) and runs real memory
consolidation cron runs: the job is created by the installed
`task.consolidate_memory` handler through the daemon's TaskManager, the
daemon's own `attach_cron_scheduler(runtime=...)` executes it, and each run is
queued with `TaskManager.run_task_now`. The recovery steps of each run are
recorded per intent by call-through recorders (probe_449_ext.StepRecorder):
identity check, authorization, capability query, source read, extraction,
bundle write (memctl, service, store), result write.

  U  `reference-sqlite` (bundled backend, no `apply_capture_bundle`): one SDK
     turn, then RUNS consolidation runs (package default batch_limit).
  R  the same runtime home reopened with the built-in store
     (AuditedMemoryStore over SQLiteMemoryStore), REOPEN_RUNS more runs.
  A  the audit wrapper over a store without the method (`store.backend: mock`,
     AuditedMemoryStore over InMemoryMemoryStore): one SDK turn, two runs; then
     the same home reopened with the built-in SQLite store, two runs.
  D  identity/authorization first: `reference-sqlite`, an identity profile with
     the hard constraint "NEVER write memory records", and one tampered
     turn.outcome intent (non-canonical capture id, appended with the runtime
     session store's append_event); two runs; then the same home reopened with
     the capable built-in store and the same profile, one run.
  N1 the bundled reference backend registered under the external adapter name
     `sqlite`: one SDK turn, one run.
  F  capable built-in SQLite store with a real write failure: a BEFORE INSERT
     trigger on `memory_capture_bundle_receipts` makes SQLite refuse the
     receipt insert. One SDK turn and run 1 with the trigger, then the trigger
     is dropped and runs 2 and 3 follow.
  PR, PF  (only with OPENMINION_TEST_POSTGRES_URL, the Linux runner): R with
     the reopened store on PostgreSQL (PostgresMemoryStore in its own schema),
     and F with the trigger in PostgreSQL.
  N2 a subclass of the bundled backend, with the same class name, whose own
     `apply_capture_bundle` raises, registered under the adapter name
     `reference-sqlite` (last, since it replaces that registration): one SDK
     turn, two runs.

Not production behaviour, said plainly: the trigger (F, PF), the identity
profile (D), the tampered intent (D) and the failing backend (N2) are set up by
the probe; the job is created by calling the tool's handler directly.

Usage: python -I -X utf8 probe_449.py <work_dir> <out.json> [baseline_0114.json] [runs] [reopen_runs]
"""

from __future__ import annotations

import inspect
import json
import sys
import time
from importlib import metadata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
BASELINE = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 and sys.argv[3] not in ("", "-") else None
RUNS = int(sys.argv[4]) if len(sys.argv) > 4 else 14
REOPEN_RUNS = int(sys.argv[5]) if len(sys.argv) > 5 else 3
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()


def _dist(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


from probe_449_ext import (  # noqa: E402
    PG_BASE,
    REFUSE_MESSAGE,
    PgSchema,
    StepRecorder,
    count_steps,
    create_job,
    external_memory,
    intents,
    mock_memory,
    pending,
    pg_server_info,
    register_failing_backend,
    run_job,
    sqlite_count,
    sqlite_trigger,
    steps_by_root,
    turn,
    turn_diagnostics,
)

info = banner("probe_449",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic", "httpx", "openai",
                                                   "SQLAlchemy", "psycopg", "psycopg-binary")},
              runs=RUNS, reopen_runs=REOPEN_RUNS,
              baseline=f"{BASELINE.parent.name}/{BASELINE.name}" if BASELINE else None, **pg_server_info())

from openminion import Agent  # noqa: E402
from openminion.modules.memory.backends.external import register_reference_sqlite_backend  # noqa: E402
from openminion.tools.task.constants import DEFAULT_CONSOLIDATION_BATCH_LIMIT  # noqa: E402
from probe_449_lib import AGENT_ID, CAPTURE_MESSAGE, Cron, Observers, ScriptedProvider, build_runtime  # noqa: E402
from probe_449_lib import chdir_env, restore_env  # noqa: E402

c = Checks()
DENY_CONSTRAINT = "NEVER write memory records"
STEP_NAMES = ("identity", "authorize", "capability", "source", "extract", "bundle.store", "bundle", "result")


# ---------------------------------------------------------------- one runtime per phase
class Phase:
    def __init__(self, case: str, phase: str, root: Path, labels: dict[str, str], *, external: str | None = None,
                 memory: Any = None, identity: str | None = None) -> None:
        self.case, self.phase, self.root, self.labels = case, phase, root, labels
        self.provider = ScriptedProvider()
        self.old_env = chdir_env(root)
        self.runtime = self.obs = self.rec = self.cron = None
        self.row: dict[str, Any] = {"case": case, "phase": phase, "runs": []}
        try:
            self.runtime = build_runtime(root, self.provider, external_adapter=external,
                                         identity_constraint=identity, memory_config=memory)
            self.obs = Observers(self.runtime).install()
            self.rec = StepRecorder(self.runtime, self.obs).install()
            store = self.obs.assembly.service._store
            backend = getattr(store, "_backend", None)
            inner = getattr(store, "_store", None)
            self.inner = inner
            self.row["classes"] = {
                "memctl": type(self.obs.memctl).__name__, "memory_service": type(self.obs.assembly.service).__name__,
                "memory_store": type(store).__name__,
                "memory_backend": None if backend is None else f"{type(backend).__module__}.{type(backend).__name__}",
                "memory_store_inner": None if inner is None else type(inner).__name__,
                "runtime_sessions": type(self.runtime.sessions).__name__}
            target = backend if backend is not None else inner
            self.row["store_has_callable_apply_capture_bundle"] = callable(getattr(target, "apply_capture_bundle",
                                                                                   None))
            self.row["memctl_supports_capture_bundles"] = self.capability()
            self.row["external_adapter"] = external
            self.row["identity_constraints_loaded"] = list(
                getattr(self.obs.security_policy, "_identity_constraints", []) or [])
            self.cron = Cron(self.runtime, root)
        except Exception:
            self.close()
            raise

    def capability(self) -> Any:
        predicate = getattr(self.obs.memctl, "supports_capture_bundles", None)
        if not callable(predicate):
            return "absent"
        try:
            return bool(predicate())
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    def memory_db(self) -> Path | None:
        db = getattr(self.inner, "db_path", None)
        return Path(str(db)) if db else None

    def user_turn(self, session: str = "capture") -> dict[str, Any]:
        before = {i["capture_id"] for i in intents(self.runtime)}
        mark, pmark = self.rec.mark(), len(self.provider.requests)
        with Agent(runtime=self.runtime) as agent:
            out = turn(lambda: agent.run(CAPTURE_MESSAGE, session_id=session, timeout_seconds=120))
        new = [i for i in intents(self.runtime) if i["capture_id"] not in before]
        for i in new:
            self.labels.setdefault(str(i["root_turn_id"]), "user" if session == "capture" else session)
        out["provider_requests"] = [r["kind"] for r in self.provider.requests[pmark:]]
        out["own_bundle_write_steps"] = [s["step"] + (f" raised {s['raised'].split(':')[0]}" if "raised" in s else "")
                                         for s in self.rec.since(mark) if s["step"].startswith("bundle")]
        out["new_intents"] = [{k: i[k] for k in ("capture_state", "has_result", "results")} for i in new]
        out["diagnostics"] = [d for d in turn_diagnostics(self.runtime)
                              if d["capture_id"] in {i["capture_id"] for i in new}]
        return out

    def user(self) -> dict[str, Any] | None:
        return next((i for i in intents(self.runtime) if self.labels.get(str(i["root_turn_id"])) == "user"), None)

    def start(self) -> None:
        self.cron.start()

    def run(self, job_id: str, index: int, note: str | None = None) -> dict[str, Any]:
        rt = self.runtime
        before = intents(rt)
        user_before = self.user()
        diag_before = [d for d in turn_diagnostics(rt) if user_before and d["capture_id"] == user_before["capture_id"]]
        self.rec.capture_roots = {i["capture_id"]: i["root_turn_id"] for i in before}
        mark, pmark, rmark = self.rec.mark(), len(self.provider.requests), len(self.obs.recoveries)
        done = run_job(self.cron, self.obs, job_id)
        after = intents(rt)
        known = {i["capture_id"] for i in before}
        new = [i for i in after if i["capture_id"] not in known]
        for i in new:
            self.labels.setdefault(str(i["root_turn_id"]), f"scheduled turn {self.phase} run {index}")
        steps = self.rec.since(mark)
        recs = self.obs.recoveries[rmark:]
        windows = [(r["t_start"], r["t_end"]) for r in recs]
        rec_kinds = [k for s, e in windows for k in self.provider.kinds_between(s, e)]
        kinds = [r["kind"] for r in self.provider.requests[pmark:]]
        user_now = self.user()
        diag_after = [d for d in turn_diagnostics(rt) if user_now and d["capture_id"] == user_now["capture_id"]]
        row = {
            "run": index, "note": note, **{k: done.get(k) for k in ("state", "error", "summary", "probe_wait")},
            "pending_before": len(pending(before)),
            "pending_before_labels": [self.labels.get(str(i["root_turn_id"]), "?") for i in pending(before)],
            "recovery": [r.get("result") for r in recs],
            "recovery_raised": [r.get("raised") for r in recs if r.get("raised")],
            "steps_by_intent": steps_by_root(steps, self.labels),
            "step_counts": {k: count_steps(steps, k) for k in STEP_NAMES},
            "recovery_provider_extractions": rec_kinds.count("extraction"),
            "provider_requests_by_kind": {k: kinds.count(k) for k in sorted(set(kinds))},
            "new_intents": [{"label": self.labels.get(str(i["root_turn_id"])), "capture_state": i["capture_state"],
                             "results": i["results"], "result_disposition": i["result_disposition"]} for i in new],
            "pending_after": len(pending(after)),
            "user_intent": None if user_now is None else {k: user_now[k] for k in ("capture_state", "has_result",
                                                                                   "results")},
            "user_intent_payload_unchanged": bool(user_now and user_before and user_now["payload"]
                                                  == user_before["payload"]),
            "user_diagnostic_unchanged": diag_after == diag_before,
            "user_diagnostic": diag_after,
        }
        self.row["runs"].append(row)
        print(f"{self.case}/{self.phase} run {index}: state={row['state']} pending_before={row['pending_before']} "
              f"recovery={row['recovery']} raised={row['recovery_raised']} steps={row['steps_by_intent']} "
              f"recovery_extractions={row['recovery_provider_extractions']} pending_after={row['pending_after']} "
              f"user={row['user_intent']}", flush=True)
        return row

    def close(self) -> dict[str, Any]:
        if self.cron is not None:
            try:
                self.cron.stop()
            except Exception as exc:  # noqa: BLE001
                self.row["scheduler_stop"] = f"{type(exc).__name__}: {exc}"
        if self.rec is not None:
            self.rec.uninstall()
        if self.obs is not None:
            self.obs.uninstall()
        if self.runtime is not None:
            try:
                self.runtime.close()
            except Exception as exc:  # noqa: BLE001
                self.row["runtime_close"] = f"{type(exc).__name__}: {exc}"
        restore_env(self.old_env)
        self.row["provider_requests_total"] = len(self.provider.requests)
        kinds = [r["kind"] for r in self.provider.requests]
        self.row["provider_requests_by_kind"] = {k: kinds.count(k) for k in sorted(set(kinds))}
        self.row["provider_hosts"] = sorted({self.provider.base_url.split("/")[2].split(":")[0]})
        self.provider.close()
        return self.row


def guarded(name: str, body: Any) -> dict[str, Any]:
    """Run one case; an unexpected exception is recorded (the checks then fail)."""
    try:
        return body()
    except Exception as exc:  # noqa: BLE001
        import traceback

        print(f"{name}: case error {type(exc).__name__}: {exc}", flush=True)
        return {"case_error": f"{type(exc).__name__}: {exc}",
                "case_error_frames": [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                      for f in traceback.extract_tb(exc.__traceback__)][-8:]}


def runs_phase(p: Phase, job_id: str, count: int, *, start: int = 1) -> None:
    for index in range(start, start + count):
        p.run(job_id, index)


# ---------------------------------------------------------------- U + R
def case_u_r() -> dict[str, Any]:
    root = WORK / "U"
    labels: dict[str, str] = {}
    out: dict[str, Any] = {}
    p = Phase("U", "U", root, labels, external="reference-sqlite")
    try:
        out["user_turn"] = p.user_turn()
        created = create_job(p.cron)
        job_id = str(created.get("task_id"))
        job = p.cron.store.get_cron_job(job_id) or {}
        out["job"] = {"created": {k: created.get(k) for k in ("ok", "task_id", "batch_limit", "target_scope")},
                      "batch_limit": ((job.get("payload") or {}).get("_openminion_memory_consolidation") or {})
                      .get("batch_limit")}
        p.start()
        runs_phase(p, job_id, RUNS)
    finally:
        out["U"] = p.close()
    p = Phase("U", "R", root, labels)
    try:
        p.start()
        out["R_receipts_before"] = sqlite_count(p.memory_db(), "memory_capture_bundle_receipts")
        runs_phase(p, job_id, REOPEN_RUNS)
        user = p.user()
        out["R_receipts_for_user"] = sqlite_count(p.memory_db(), "memory_capture_bundle_receipts",
                                                  "WHERE capture_id = ?", (user["capture_id"],)) if user else None
    finally:
        out["R"] = p.close()
    out["labels"] = labels
    return out


# ---------------------------------------------------------------- A
def case_a() -> dict[str, Any]:
    root = WORK / "A"
    labels: dict[str, str] = {}
    out: dict[str, Any] = {}
    p = Phase("A", "A", root, labels, memory=mock_memory())
    try:
        out["user_turn"] = p.user_turn()
        job_id = str(create_job(p.cron).get("task_id"))
        p.start()
        runs_phase(p, job_id, 2)
    finally:
        out["A"] = p.close()
    p = Phase("A", "A-reopened", root, labels)
    try:
        p.start()
        runs_phase(p, job_id, 2, start=3)
    finally:
        out["A_reopened"] = p.close()
    out["labels"] = labels
    return out


# ---------------------------------------------------------------- D
def case_d() -> dict[str, Any]:
    root = WORK / "D"
    labels: dict[str, str] = {"root-tampered": "tampered"}
    out: dict[str, Any] = {}
    p = Phase("D", "D", root, labels, external="reference-sqlite", identity=DENY_CONSTRAINT)
    try:
        out["user_turn"] = p.user_turn()
        appended = p.runtime.sessions.append_event(
            session_id="capture", event_type="turn.outcome", canonical_event_id="turn.outcome:tampered",
            payload={"agent_id": AGENT_ID, "capture_id": "capture:tampered", "root_turn_id": "root-tampered",
                     "capture_state": "pending", "runtime_session_id": "capture"})
        out["tampered_intent"] = {"canonical_event_id": getattr(appended, "canonical_event_id", None),
                                  "selected_as_pending": any(i["capture_id"] == "capture:tampered"
                                                             for i in pending(intents(p.runtime)))}
        job_id = str(create_job(p.cron).get("task_id"))
        p.start()
        runs_phase(p, job_id, 2)
        out["D_policy_decisions"] = [{k: d.get(k) for k in ("decision", "reason_code", "allowed", "target")}
                                     for d in p.obs.policy_decisions]
    finally:
        out["D"] = p.close()
    p = Phase("D", "D-capable", root, labels, identity=DENY_CONSTRAINT)
    try:
        p.start()
        runs_phase(p, job_id, 1, start=3)
        out["D_capable_policy_decisions"] = [{k: d.get(k) for k in ("decision", "reason_code", "allowed", "target")}
                                             for d in p.obs.policy_decisions]
    finally:
        out["D_capable"] = p.close()
    out["labels"] = labels
    return out


# ---------------------------------------------------------------- N1, N2
def case_n(name: str, adapter: str, runs: int) -> dict[str, Any]:
    root = WORK / name
    labels: dict[str, str] = {}
    out: dict[str, Any] = {}
    p = Phase(name, name, root, labels, external=adapter)
    try:
        out["user_turn"] = p.user_turn()
        job_id = str(create_job(p.cron).get("task_id"))
        p.start()
        runs_phase(p, job_id, runs)
    finally:
        out[name] = p.close()
    out["labels"] = labels
    return out


# ---------------------------------------------------------------- F (SQLite) and PF (PostgreSQL)
def case_f(name: str, pg: PgSchema | None = None) -> dict[str, Any]:
    root = WORK / name
    labels: dict[str, str] = {}
    out: dict[str, Any] = {}
    p = Phase(name, name, root, labels, memory=pg.memory_config if pg is not None else None)
    try:
        db = p.memory_db()

        def receipts(capture_id: str | None = None) -> Any:
            if pg is not None:
                return pg.count("memory_capture_bundle_receipts",
                                "WHERE capture_id = :c" if capture_id else "", {"c": capture_id})
            return sqlite_count(db, "memory_capture_bundle_receipts", "WHERE capture_id = ?" if capture_id else "",
                                (capture_id,) if capture_id else ())

        def trigger(on: bool) -> str:
            return pg.trigger(on) if pg is not None else sqlite_trigger(db, on)

        if pg is not None:
            out["postgres_schema_tables"] = pg.tables()
        out["trigger_on"] = trigger(True)
        out["user_turn"] = p.user_turn()
        user = p.user()
        out["receipts_after_turn"] = receipts(user["capture_id"]) if user else None
        job_id = str(create_job(p.cron).get("task_id"))
        p.start()
        p.run(job_id, 1, note="trigger refuses the receipt insert")
        out["receipts_after_run_1"] = receipts(user["capture_id"]) if user else None
        out["trigger_off"] = trigger(False)
        p.run(job_id, 2, note="trigger dropped")
        p.run(job_id, 3)
        out["receipts_for_user_after_run_3"] = receipts(user["capture_id"]) if user else None
        out["receipts_total_after_run_3"] = receipts()
        if pg is not None:
            out["postgres_candidates_for_user"] = pg.count(
                "memory_candidates", "WHERE CAST(meta_json AS jsonb)->>'capture_id' = :c",
                {"c": user["capture_id"]}) if user else None
    finally:
        out[name] = p.close()
    out["labels"] = labels
    return out


# ---------------------------------------------------------------- PR (PostgreSQL)
def case_pr(pg: PgSchema) -> dict[str, Any]:
    root = WORK / "PR"
    labels: dict[str, str] = {}
    out: dict[str, Any] = {}
    p = Phase("PR", "PR-U", root, labels, external="reference-sqlite")
    try:
        out["user_turn"] = p.user_turn()
        job_id = str(create_job(p.cron).get("task_id"))
        p.start()
        runs_phase(p, job_id, 2)
    finally:
        out["PR_U"] = p.close()
    p = Phase("PR", "PR-R", root, labels, memory=pg.memory_config)
    try:
        out["postgres_schema_tables"] = pg.tables()
        p.start()
        runs_phase(p, job_id, REOPEN_RUNS, start=3)
        user = p.user()
        out["postgres_receipts_for_user"] = pg.count("memory_capture_bundle_receipts", "WHERE capture_id = :c",
                                                     {"c": user["capture_id"]}) if user else None
        out["postgres_candidates_for_user"] = pg.count(
            "memory_candidates", "WHERE CAST(meta_json AS jsonb)->>'capture_id' = :c",
            {"c": user["capture_id"]}) if user else None
        out["postgres_activity_during_case"] = pg.rows(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
    finally:
        out["PR_R"] = p.close()
    out["labels"] = labels
    return out


# ---------------------------------------------------------------- run the cases
register_reference_sqlite_backend()
register_reference_sqlite_backend("sqlite")  # N1: the bundled backend under a built-in store's name
observed: dict[str, Any] = {}
observed["U_R"] = guarded("U_R", case_u_r)
observed["A"] = guarded("A", case_a)
observed["D"] = guarded("D", case_d)
observed["N1"] = guarded("N1", lambda: case_n("N1", "sqlite", 1))
observed["F"] = guarded("F", lambda: case_f("F"))
if PG_BASE:
    for key, fn in (("PR", case_pr), ("PF", lambda pg: case_f("PF", pg))):
        schema = PgSchema(key)
        try:
            observed[key] = guarded(key, lambda: fn(schema))
            observed[key]["schema"] = schema.schema
        finally:
            schema.drop()
else:
    observed["PR"] = observed["PF"] = {"not_run": "OPENMINION_TEST_POSTGRES_URL is not set (no PostgreSQL here)"}
register_failing_backend("reference-sqlite")  # N2: replaces the bundled registration, so it runs last
observed["N2"] = guarded("N2", lambda: case_n("N2", "reference-sqlite", 2))

# The installed predicates, as source text (owner-local method checks; no backend names).
predicate_sources: dict[str, Any] = {}
try:
    from openminion.modules.brain.adapters.memory.runtime import MemctlAdapter
    from openminion.modules.memory.backends.builtin import BackendMemoryStoreAdapter
    from openminion.modules.memory.service.mutations import MemoryServiceMutationMixin
    from openminion.modules.memory.storage.audit import AuditedMemoryStore

    owners = {"MemctlAdapter": MemctlAdapter, "MemoryServiceMutationMixin": MemoryServiceMutationMixin,
              "AuditedMemoryStore": AuditedMemoryStore, "BackendMemoryStoreAdapter": BackendMemoryStoreAdapter}
    for name, cls in owners.items():
        fn = getattr(cls, "supports_capture_bundles", None)
        predicate_sources[name] = inspect.getsource(fn) if fn else "absent"
    import openminion.modules.memory.runtime.capture_bundle as cb

    fn = getattr(cb, "capture_bundles_supported", None)
    predicate_sources["capture_bundles_supported"] = inspect.getsource(fn) if fn else "absent"
    import openminion.modules.memory.runtime.capture_recovery as cr

    predicate_sources["_recover_capture"] = inspect.getsource(cr._recover_capture)
except Exception as exc:  # noqa: BLE001
    predicate_sources["error"] = f"{type(exc).__name__}: {exc}"
observed["predicate_sources"] = predicate_sources


# ---------------------------------------------------------------- checks
def rows(case: dict[str, Any], phase: str) -> list[dict[str, Any]]:
    return list((case.get(phase) or {}).get("runs") or [])


def steps_of(run: dict[str, Any], who: str) -> list[str]:
    return list((run.get("steps_by_intent") or {}).get(who, []))


def none_after_capability(counts: dict[str, int]) -> bool:
    return all(counts.get(k, 0) == 0 for k in ("source", "extract", "bundle.store", "bundle", "result"))


ur = observed["U_R"]
LIMIT = (ur.get("job") or {}).get("batch_limit") or DEFAULT_CONSOLIDATION_BATCH_LIMIT


def scans_all(x: dict[str, Any], recovered: bool) -> bool:
    """The run scans every pending intent (up to batch_limit) and recovers all of them, or none."""
    n = min(x.get("pending_before") or 0, LIMIT)
    return x.get("recovery") == [[n, n if recovered else 0, 0 if recovered else n]]


u_phase = ur.get("U") or {}
# The capability query exists only in packages with #468; the checks of behavior that #468 keeps (a capable store,
# authorization) expect it where the installed memory API has the predicate, so they also hold on 0.1.14.
HAS_PREDICATE = u_phase.get("memctl_supports_capture_bundles") != "absent"
ASKED_UNSUPPORTED = ["identity", "authorize=True", "capability=False"]
RECOVERED = [s for s in ["identity", "authorize=True", "capability=True", "source", "extract", "bundle.store", "bundle",
                         "result"] if HAS_PREDICATE or not s.startswith("capability")]
FAILED_WRITE_PREFIX = RECOVERED[:5] if HAS_PREDICATE else RECOVERED[:4]
DENIED = ["identity", "authorize=False"]

u_runs, r_runs = rows(ur, "U"), rows(ur, "R")
limit = (ur.get("job") or {}).get("batch_limit")
c.check("U. setup: the bundled reference-sqlite backend (SophiaGraphSqliteStore behind BackendMemoryStoreAdapter) "
        "has no callable apply_capture_bundle; the SDK turn completes with one extraction request and its capture "
        "stays pending with CAPTURE_RECOVERY_UNSUPPORTED (also in the turn's message metadata)",
        str((u_phase.get("classes") or {}).get("memory_backend", "")).endswith("SophiaGraphSqliteStore")
        and u_phase.get("store_has_callable_apply_capture_bundle") is False
        and (ur.get("user_turn") or {}).get("run_state") == "completed"
        and (ur["user_turn"].get("memory_capture_bundle_result") or {}).get("error_code")
        == "CAPTURE_RECOVERY_UNSUPPORTED"
        and ur["user_turn"].get("provider_requests", []).count("extraction") == 1
        and ur["user_turn"].get("new_intents") == [{"capture_state": "pending", "has_result": False, "results": 0}]
        and [d.get("error_code") for d in ur["user_turn"].get("diagnostics", [])] == ["CAPTURE_RECOVERY_UNSUPPORTED"],
        {"classes": u_phase.get("classes"), "user_turn": ur.get("user_turn")})
c.check("U. the memory API answers supports_capture_bundles() False for this store",
        u_phase.get("memctl_supports_capture_bundles") is False, u_phase.get("memctl_supports_capture_bundles"))
c.check(f"U. the job has the package default batch_limit ({DEFAULT_CONSOLIDATION_BATCH_LIMIT}) and all {RUNS} "
        "consolidation runs finish",
        limit == DEFAULT_CONSOLIDATION_BATCH_LIMIT and len(u_runs) == RUNS
        and all(x.get("state") == "finished" for x in u_runs),
        {"batch_limit": limit, "states": [x.get("state") for x in u_runs]})
c.check("U. repeated recovery keeps its counts: each run scans the pending intents (up to batch_limit) and reports "
        "them all pending, recovering none",
        bool(u_runs) and all(x.get("recovery") == [[min(x["pending_before"], limit or 0), 0,
                                                    min(x["pending_before"], limit or 0)]]
                             and x["pending_before"] >= 1 for x in u_runs),
        [{k: x.get(k) for k in ("run", "pending_before", "recovery")} for x in u_runs])
c.check("U. per pending intent, recovery makes only the identity check, the authorization (allowed) and the "
        "capability query (False): in every run no source read, no extractor call, no provider extraction request, "
        "no bundle write (memctl or store) and no result write",
        bool(u_runs) and all(all(v == ASKED_UNSUPPORTED for v in (x.get("steps_by_intent") or {}).values())
                             and len(x.get("steps_by_intent") or {}) == min(x["pending_before"], limit or 0)
                             and none_after_capability(x.get("step_counts") or {})
                             and x.get("recovery_provider_extractions") == 0 for x in u_runs),
        [{k: x.get(k) for k in ("run", "steps_by_intent", "step_counts", "recovery_provider_extractions")}
         for x in u_runs])
c.check("U. the original intent stays pending after every run: no memory.capture.result, its turn.outcome payload "
        "unchanged and its CAPTURE_RECOVERY_UNSUPPORTED diagnostic (message metadata) unchanged",
        bool(u_runs) and all(x.get("user_intent") == {"capture_state": "pending", "has_result": False, "results": 0}
                             and x.get("user_intent_payload_unchanged") is True
                             and x.get("user_diagnostic_unchanged") is True
                             and [d.get("error_code") for d in x.get("user_diagnostic", [])]
                             == ["CAPTURE_RECOVERY_UNSUPPORTED"] for x in u_runs),
        [{k: x.get(k) for k in ("run", "user_intent", "user_intent_payload_unchanged", "user_diagnostic_unchanged")}
         for x in u_runs])
r_phase = ur.get("R") or {}
c.check("R. the same runtime home reopened with the built-in store (AuditedMemoryStore over SQLiteMemoryStore): "
        "supports_capture_bundles() is True",
        r_phase.get("classes", {}).get("memory_store") == "AuditedMemoryStore"
        and r_phase.get("classes", {}).get("memory_store_inner") == "SQLiteMemoryStore"
        and r_phase.get("memctl_supports_capture_bundles") is True,
        {"classes": r_phase.get("classes"), "supports": r_phase.get("memctl_supports_capture_bundles")})
first = r_runs[0] if r_runs else {}
c.check("R. the first reopened run recovers the original intent once: its steps are identity, authorize, capability "
        "(True), source, extraction, bundle write (the audit wrapper store, then memctl) and result write; every "
        "scanned pending intent (here only the original one) is recovered with one provider extraction request "
        "each; one memory.capture.result and one receipt row for the original intent",
        first.get("state") == "finished" and scans_all(first, True)
        and steps_of(first, "user") == RECOVERED
        and all(v == RECOVERED for v in (first.get("steps_by_intent") or {}).values())
        and first.get("recovery_provider_extractions") == min(first.get("pending_before") or 0, LIMIT)
        and first.get("user_intent") == {"capture_state": "pending", "has_result": True, "results": 1}
        and ur.get("R_receipts_for_user") == 1,
        {"run": {k: first.get(k) for k in ("state", "pending_before", "recovery", "steps_by_intent",
                                            "recovery_provider_extractions", "user_intent")},
         "receipts": ur.get("R_receipts_for_user")})
c.check("R. the later runs do nothing: scanned 0, no recovery step at all, no extraction, still exactly one result",
        len(r_runs) == REOPEN_RUNS and all(x.get("state") == "finished" and x.get("recovery") == [[0, 0, 0]]
                                           and x.get("steps_by_intent") == {}
                                           and x.get("recovery_provider_extractions") == 0
                                           and (x.get("user_intent") or {}).get("results") == 1
                                           for x in r_runs[1:]),
        [{k: x.get(k) for k in ("run", "recovery", "steps_by_intent", "user_intent")} for x in r_runs[1:]])
user_extract_runs = [f"{ph} run {x['run']}" for ph, rr in (("U", u_runs), ("R", r_runs)) for x in rr
                     if any(s.startswith("extract") for s in steps_of(x, "user"))]
c.check("U+R. over all runs the original intent is extracted exactly once, in the first reopened run",
        user_extract_runs == ["R run 1"], user_extract_runs)

a = observed["A"]
a_phase, a_runs, ar_runs = a.get("A") or {}, rows(a, "A"), rows(a, "A_reopened")
c.check("A. audit wrapper over a store without apply_capture_bundle (AuditedMemoryStore over InMemoryMemoryStore, "
        "store.backend mock): supports_capture_bundles() is False; the SDK turn leaves one pending intent",
        a_phase.get("classes", {}).get("memory_store") == "AuditedMemoryStore"
        and a_phase.get("classes", {}).get("memory_store_inner") == "InMemoryMemoryStore"
        and a_phase.get("store_has_callable_apply_capture_bundle") is False
        and a_phase.get("memctl_supports_capture_bundles") is False
        and (a.get("user_turn") or {}).get("new_intents") == [{"capture_state": "pending", "has_result": False,
                                                                "results": 0}],
        {"classes": a_phase.get("classes"), "supports": a_phase.get("memctl_supports_capture_bundles"),
         "user_turn": a.get("user_turn")})
c.check("A. both runs finish and report the intent pending: identity, authorize, capability (False) only; no source "
        "read, extraction, bundle write or result write",
        len(a_runs) == 2 and all(x.get("state") == "finished" and scans_all(x, False)
                                 and steps_of(x, "user") == ASKED_UNSUPPORTED
                                 and none_after_capability(x.get("step_counts") or {})
                                 and x.get("recovery_provider_extractions") == 0 for x in a_runs),
        [{k: x.get(k) for k in ("run", "state", "error", "recovery", "recovery_raised", "steps_by_intent",
                                "recovery_provider_extractions")} for x in a_runs])
c.check("A. reopened with the built-in SQLite store: the first run recovers the intent once (one extraction, one "
        "result), the next does nothing",
        len(ar_runs) == 2 and ar_runs[0].get("recovery") == [[1, 1, 0]]
        and ar_runs[0].get("steps_by_intent") == {"user": RECOVERED}
        and ar_runs[0].get("recovery_provider_extractions") == 1
        and ar_runs[1].get("recovery") == [[0, 0, 0]] and ar_runs[1].get("steps_by_intent") == {}
        and (ar_runs[1].get("user_intent") or {}).get("results") == 1,
        [{k: x.get(k) for k in ("run", "recovery", "steps_by_intent", "recovery_provider_extractions",
                                "user_intent")} for x in ar_runs])

d = observed["D"]
d_phase, d_runs, dc_runs = d.get("D") or {}, rows(d, "D"), rows(d, "D_capable")


def denied_first(x: dict[str, Any]) -> bool:
    steps = x.get("steps_by_intent") or {}
    return (x.get("state") == "finished" and scans_all(x, False) and steps.get("tampered") == ["identity"]
            and steps.get("user") == DENIED and all(v == DENIED for k, v in steps.items() if k != "tampered")
            and (x.get("step_counts") or {}).get("capability") == 0
            and none_after_capability(x.get("step_counts") or {}) and x.get("recovery_provider_extractions") == 0)


c.check("D. setup: the identity profile's hard constraint is loaded, the SDK turn leaves the reference-sqlite intent "
        "pending, and the tampered intent (non-canonical capture id) is selected as pending",
        DENY_CONSTRAINT in (d_phase.get("identity_constraints_loaded") or [])
        and ((d.get("user_turn") or {}).get("memory_capture_bundle_result") or {}).get("error_code")
        == "CAPTURE_RECOVERY_UNSUPPORTED"
        and (d.get("tampered_intent") or {}).get("selected_as_pending") is True,
        {"constraints": d_phase.get("identity_constraints_loaded"), "user_turn": d.get("user_turn"),
         "tampered": d.get("tampered_intent")})
c.check("D. identity and authorization come first: in each run the tampered intent stops at the identity check "
        "(never authorized) and every other intent at the denied authorization (identity_hard_constraint_restricted); "
        "none reaches the capability query; all reported pending; no source read, extraction, bundle or result write",
        len(d_runs) == 2 and all(denied_first(x) for x in d_runs)
        and bool(d.get("D_policy_decisions"))
        and all(not x.get("allowed") for x in d.get("D_policy_decisions", [])),
        {"runs": [{k: x.get(k) for k in ("run", "recovery", "steps_by_intent", "step_counts")} for x in d_runs],
         "policy": d.get("D_policy_decisions")})
dcap = d.get("D_capable") or {}
c.check("D. no bypass with a capable store: reopened with the built-in store (callable apply_capture_bundle) and the "
        "same profile, the run gives the same steps, reports every intent pending and extracts nothing",
        dcap.get("store_has_callable_apply_capture_bundle") is True
        and dcap.get("memctl_supports_capture_bundles") in (True, "absent")
        and len(dc_runs) == 1 and denied_first(dc_runs[0])
        and all(not x.get("allowed") for x in d.get("D_capable_policy_decisions", [])),
        {"supports": dcap.get("memctl_supports_capture_bundles"),
         "run": [{k: x.get(k) for k in ("recovery", "steps_by_intent")} for x in dc_runs],
         "policy": d.get("D_capable_policy_decisions")})

n1, n2 = observed["N1"], observed["N2"]
n1_phase, n2_phase = n1.get("N1") or {}, n2.get("N2") or {}
n1_runs, n2_runs = rows(n1, "N1"), rows(n2, "N2")
c.check("N. no backend-name inference: the bundled backend registered under the adapter name 'sqlite' answers "
        "False and is not extracted (identity, authorize, capability only); a backend with the same adapter name "
        "and class name as the bundled one ('reference-sqlite', SophiaGraphSqliteStore) but its own "
        "apply_capture_bundle answers True",
        n1_phase.get("memctl_supports_capture_bundles") is False and len(n1_runs) == 1
        and steps_of(n1_runs[0], "user") == ASKED_UNSUPPORTED
        and n1_runs[0].get("recovery_provider_extractions") == 0
        and str((n2_phase.get("classes") or {}).get("memory_backend", "")).endswith(".SophiaGraphSqliteStore")
        and n2_phase.get("external_adapter") == u_phase.get("external_adapter") == "reference-sqlite"
        and n2_phase.get("memctl_supports_capture_bundles") is True,
        {"N1": {k: n1_phase.get(k) for k in ("external_adapter", "classes", "memctl_supports_capture_bundles")},
         "N1_run": [{k: x.get(k) for k in ("recovery", "steps_by_intent", "recovery_provider_extractions")}
                    for x in n1_runs],
         "N2": {k: n2_phase.get(k) for k in ("external_adapter", "classes", "memctl_supports_capture_bundles")},
         "U": {k: u_phase.get(k) for k in ("external_adapter", "classes", "memctl_supports_capture_bundles")}})
names = ("reference", "sophiagraph", "sqlite", "postgres", "external_adapter", "__name__", "provider")
pred_text = "\n".join(v for k, v in predicate_sources.items() if k not in ("_recover_capture", "error"))
c.check("N. the installed predicates are owner-local method checks: no backend, adapter or class name appears in "
        "their source",
        bool(pred_text) and "absent" not in predicate_sources.values()
        and not any(n in pred_text.lower() for n in names),
        {k: v.splitlines()[0] if isinstance(v, str) and v else v for k, v in predicate_sources.items()})
c.check("N2. a capable third-party store whose own write fails keeps the existing behavior: the SDK turn raises "
        "the backend's error unchanged and leaves the intent pending; each run reads and extracts the intent once, "
        "the bundle write raises the same error, recovery counts it pending and no result is written",
        str((n2.get("user_turn") or {}).get("raised", "")).startswith("ProbeBackendCaptureFailed")
        and (n2.get("user_turn") or {}).get("new_intents") == [{"capture_state": "pending", "has_result": False,
                                                                 "results": 0}]
        and len(n2_runs) == 2
        and all(scans_all(x, False) and steps_of(x, "user")[:len(FAILED_WRITE_PREFIX)] == FAILED_WRITE_PREFIX
                and any(s.startswith("bundle") and "ProbeBackendCaptureFailed" in s for s in steps_of(x, "user"))
                and "result" not in steps_of(x, "user")
                and (x.get("user_intent") or {}).get("results") == 0 for x in n2_runs),
        {"user_turn": n2.get("user_turn"),
         "runs": [{k: x.get(k) for k in ("run", "state", "error", "recovery", "steps_by_intent",
                                         "recovery_provider_extractions", "user_intent")} for x in n2_runs]})


def failure_case_checks(key: str, label: str) -> None:
    f = observed[key]
    if "not_run" in f:
        return
    f_phase, f_runs = f.get(key) or {}, rows(f, key)
    c.check(f"{key}. setup: {label}, a capable store (callable apply_capture_bundle; supports_capture_bundles() True "
            "where the package has it) with the trigger in place; the SDK turn's own bundle write is refused by the "
            "database, no receipt is written and the intent stays pending",
            f_phase.get("store_has_callable_apply_capture_bundle") is True
            and f_phase.get("memctl_supports_capture_bundles") in ((True,) if HAS_PREDICATE else ("absent",))
            and "probe_refuse_capture" in str(f.get("trigger_on")) and f.get("receipts_after_turn") == 0
            and REFUSE_MESSAGE in str((f.get("user_turn") or {}).get("raised"))
            and (f.get("user_turn") or {}).get("new_intents") == [{"capture_state": "pending", "has_result": False,
                                                                     "results": 0}],
            {"classes": f_phase.get("classes"), "supports": f_phase.get("memctl_supports_capture_bundles"),
             "trigger": f.get("trigger_on"), "user_turn": f.get("user_turn"),
             "receipts": f.get("receipts_after_turn")})
    r1 = f_runs[0] if f_runs else {}
    user_steps = steps_of(r1, "user")
    c.check(f"{key}. run 1 with the trigger: the source is read and extracted (after the capability query where the "
            "package has it), the bundle write fails with the database's error, which ends the run as failed; no "
            "result and no receipt are written and the intent stays pending",
            user_steps[:len(FAILED_WRITE_PREFIX)] == FAILED_WRITE_PREFIX
            and any(s.startswith("bundle") and "raised" in s for s in user_steps) and "result" not in user_steps
            and r1.get("recovery_provider_extractions") == 1
            and REFUSE_MESSAGE in json.dumps(r1.get("recovery_raised"))
            and (r1.get("user_intent") or {}).get("results") == 0 and f.get("receipts_after_run_1") == 0,
            {k: r1.get(k) for k in ("state", "error", "summary", "recovery", "recovery_raised", "steps_by_intent",
                                    "recovery_provider_extractions", "user_intent")})
    later = f_runs[1:]
    c.check(f"{key}. after the trigger is dropped, run 2 recovers the intent once (one extraction, one result, one "
            "receipt) and run 3 does nothing",
            len(later) == 2 and later[0].get("recovery") == [[1, 1, 0]]
            and later[0].get("steps_by_intent") == {"user": RECOVERED}
            and later[0].get("recovery_provider_extractions") == 1
            and later[1].get("recovery") == [[0, 0, 0]] and later[1].get("steps_by_intent") == {}
            and (later[1].get("user_intent") or {}).get("results") == 1
            and f.get("receipts_for_user_after_run_3") == 1,
            {"runs": [{k: x.get(k) for k in ("run", "recovery", "steps_by_intent", "recovery_provider_extractions",
                                             "user_intent")} for x in later],
             "receipts": f.get("receipts_for_user_after_run_3")})


failure_case_checks("F", "built-in SQLite store (AuditedMemoryStore over SQLiteMemoryStore)")
pr = observed["PR"]
if "not_run" not in pr:
    pr_runs, pu_runs = rows(pr, "PR_R"), rows(pr, "PR_U")
    c.check("PR. before reopening, the reference-sqlite runs ask the store and do not extract (as in U)",
            len(pu_runs) == 2 and all(steps_of(x, "user") == ASKED_UNSUPPORTED
                                      and x.get("recovery_provider_extractions") == 0 for x in pu_runs),
            [{k: x.get(k) for k in ("run", "recovery", "steps_by_intent")} for x in pu_runs])
    c.check("PR. reopened with the built-in store on PostgreSQL (AuditedMemoryStore over PostgresMemoryStore, own "
            "schema): True; the first run recovers the original intent once with one extraction; its receipt and "
            "candidate are in PostgreSQL (server-side counts); the later runs do nothing",
            (pr.get("PR_R") or {}).get("classes", {}).get("memory_store_inner") == "PostgresMemoryStore"
            and (pr.get("PR_R") or {}).get("memctl_supports_capture_bundles") is True
            and len(pr_runs) == REOPEN_RUNS and scans_all(pr_runs[0], True)
            and steps_of(pr_runs[0], "user") == RECOVERED
            and all(x.get("recovery") == [[0, 0, 0]] and x.get("steps_by_intent") == {} for x in pr_runs[1:])
            and pr.get("postgres_receipts_for_user") == 1 and pr.get("postgres_candidates_for_user") == 1,
            {"classes": (pr.get("PR_R") or {}).get("classes"),
             "runs": [{k: x.get(k) for k in ("run", "recovery", "steps_by_intent", "recovery_provider_extractions")}
                      for x in pr_runs],
             "server_side": {k: pr.get(k) for k in ("postgres_receipts_for_user", "postgres_candidates_for_user",
                                                     "postgres_schema_tables")}})
    user_pr = [f"{ph} run {x['run']}" for ph, rr in (("PR-U", pu_runs), ("PR-R", pr_runs)) for x in rr
               if any(s.startswith("extract") for s in steps_of(x, "user"))]
    c.check("PR. over all runs the original intent is extracted exactly once, in the first reopened run",
            user_pr == ["PR-R run 3"], user_pr)
failure_case_checks("PF", "built-in PostgreSQL store (AuditedMemoryStore over PostgresMemoryStore)")

hosts = sorted({h for case in observed.values() if isinstance(case, dict) for ph in case.values()
                if isinstance(ph, dict) for h in ph.get("provider_hosts", [])})
c.check("Offline provider: every case's openai base URL is the scripted server on 127.0.0.1", hosts == ["127.0.0.1"],
        hosts)


# ---------------------------------------------------------------- same behavior as 0.1.14 where unchanged
def behavior(case: dict[str, Any], key: str) -> dict[str, Any]:
    """What #468 must not change for a capable store whose write fails: the SDK turn's outcome, the original intent's
    recovery steps apart from the new capability query, the error recovery raises (and the run's state and error
    when it does), and the original intent's results. The scheduled turns' own captures are #456 and left out.
    Error texts are compared by their first line: PostgreSQL's carries the statement and its per-run parameters
    (capture id, hashes, timestamps) on the following lines."""
    def first_line(text: Any) -> Any:
        return None if text is None else (str(text).splitlines() or [""])[0]

    user_turn = case.get("user_turn") or {}
    out_runs = []
    for x in rows(case, key):
        item = {"user_steps": [s for s in steps_of(x, "user") if not s.startswith("capability")],
                "recovery_raised": [first_line(r) for r in x.get("recovery_raised", [])],
                "user_results": (x.get("user_intent") or {}).get("results")}
        if x.get("recovery_raised"):
            err = x.get("error")
            item["run_state"] = x.get("state")
            item["run_error"] = ({**err, "message": first_line(err.get("message"))} if isinstance(err, dict)
                                 else first_line(err))
        out_runs.append(item)
    return {"user_turn": {"raised": first_line(user_turn.get("raised")), "run_state": user_turn.get("run_state"),
                          "own_bundle_write_steps": user_turn.get("own_bundle_write_steps"),
                          "new_intents": user_turn.get("new_intents")},
            "runs": out_runs}


observed["behavior_for_baseline"] = {k: behavior(observed[k], k) for k in ("F", "PF", "N2")
                                     if "not_run" not in observed[k]}
if BASELINE is not None and BASELINE.is_file():
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    base_beh = (base.get("observed") or {}).get("behavior_for_baseline") or {}
    for key in observed["behavior_for_baseline"]:
        if key not in base_beh:
            continue
        c.check(f"{key}. unchanged from the published 0.1.14 (same probe, its JSON as the baseline): the SDK turn's "
                "outcome and error, the original intent's recovery steps other than the new capability query, the "
                "error recovery raises with the run's state and error, and the intent's results",
                sanitize(observed["behavior_for_baseline"][key]) == base_beh[key],
                {"0.1.15": observed["behavior_for_baseline"][key], "0.1.14": base_beh[key]})


# ---------------------------------------------------------------- totals and output
def totals(case: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for ph, val in case.items():
        if isinstance(val, dict) and "runs" in val:
            out[ph] = {"runs": len(val["runs"]),
                       "recovery_extraction_requests": sum(x.get("recovery_provider_extractions") or 0
                                                           for x in val["runs"]),
                       "recovered": sum(((x.get("recovery") or [[0, 0, 0]])[0] or [0, 0, 0])[1]
                                        for x in val["runs"]),
                       "provider_requests_total": val.get("provider_requests_total"),
                       "provider_requests_by_kind": val.get("provider_requests_by_kind")}
    return out


summary_totals = {k: totals(observed[k]) for k in ("U_R", "A", "D", "N1", "F", "PR", "PF", "N2")
                  if "not_run" not in observed[k]}
write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "totals": summary_totals, "observed": sanitize(observed)})
print(json.dumps(summary_totals, indent=None, default=str))
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
