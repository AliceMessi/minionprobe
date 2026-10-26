"""#388 on the installed package: the runtime closes the external memory backend it owns.

Cyclic GC is disabled before openminion is imported and gc.collect() is counted
(it is never called by the probe), so nothing below is closed by a collection.

  A. Real SDK runtime, `memory.backend.provider: external` with an owned backend
     that has close() (the bundled reference engine plus a counting close()):
     one echo turn, then runtime.close() three times. Backend and the assembly's
     artifact store close once; the memory service holds a BackendMemoryStoreAdapter;
     no memory.store store and no audit sink are created; the artifact store
     rejects a write afterwards and its index file can be renamed and deleted.
  B. Resource-free / no-close backend: the bundled `reference-sqlite` adapter
     (no close()): same run, close completes, artifact store closes once.
     B2: an unusable `memory.store` (postgres without URL) with the external
     provider: the runtime still builds (the store is never opened).
  C. Borrowed backend ownership (installed MemoryService/BackendMemoryStoreAdapter):
     a borrowed backend is never closed and stays usable; an owned one closes once.
  D. Default built-in provider (control): one SQLite store, one audit sink, one
     artifact store, each closed once over three runtime.close() calls.
  E. Scheduler-still-running boundary (installed builder; APIRuntime cannot inject
     a scheduler): nothing closes while the scheduler is alive; after it stops,
     close succeeds once, for the external backend and for the built-in store.
  P. (only with OPENMINION_TEST_POSTGRES_URL) external provider with a PostgreSQL
     memory.store: no PostgresMemoryStore, 0 server connections for the run's
     application_name after build, after a turn and after close, no tables in its
     schema. Positive control: the default provider with the same PostgreSQL
     memory.store opens one engine, disposes it once, leaves 0 connections.

Usage: python -I -X utf8 probe_388.py <work_dir> <out.json>
"""

from __future__ import annotations

import gc

gc.disable()

import logging  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402
from probe_388_lib import (  # noqa: E402
    BACKEND_CLOSES,
    CLOSING_ADAPTER,
    CloseCounter,
    GcGuard,
    PgDatabase,
    Scheduler,
    external_config,
    register_backends,
    rename_delete,
    runtime_manager,
    write_outcome,
)

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()
GC = GcGuard()
PG_URL = os.environ.get("OPENMINION_TEST_POSTGRES_URL") or os.environ.get("PGURL") or ""
extra = {"gc_enabled_at_start": gc.isenabled()}
admin = None
if PG_URL:
    import psycopg  # noqa: E402
    import sqlalchemy as sa  # noqa: E402

    admin = sa.create_engine(PG_URL, future=True)
    with admin.connect() as _conn:
        extra["postgres_server_version"] = _conn.execute(sa.text("SHOW server_version")).scalar_one()
    extra.update(sqlalchemy=sa.__version__, psycopg=psycopg.__version__)
else:
    extra["postgres"] = "not configured: the PostgreSQL case (P) is not run here"
info = banner("probe_388", **extra)

from openminion import Agent, APIRuntime  # noqa: E402
from openminion.base.config import OpenMinionConfig  # noqa: E402
from openminion.modules.artifact.control import ArtifactCtl  # noqa: E402
from openminion.modules.memory.backends.builtin import BackendMemoryStoreAdapter  # noqa: E402
from openminion.modules.memory.models import MemoryRecord  # noqa: E402
from openminion.modules.memory.service import MemoryService  # noqa: E402
from openminion.services.runtime import memory as runtime_memory  # noqa: E402

RuntimeMemoryAssembly = runtime_memory.RuntimeMemoryAssembly
BACKENDS = register_backends()
os.environ["OPENMINION_HOME"] = str(WORK / "env-home")
os.environ["OPENMINION_DATA_ROOT"] = str(WORK / "env-home" / "data")

c = Checks()
observed: dict[str, object] = {}


def record(rid: str) -> MemoryRecord:
    return MemoryRecord(id=rid, scope="session:s1", type="fact", title=rid, content={"text": rid},
                        created_at="2026-10-26T00:00:00+00:00", updated_at="2026-10-26T00:00:00+00:00")


def run_runtime(root: Path, memory_config, *, closes: int = 3, turn: bool = True, pg: PgDatabase | None = None):
    """Build an SDK runtime with this memory config, optionally run one turn, close N times."""
    counter = CloseCounter()
    stores: list[object] = []
    sinks: list[object] = []
    ctls: list[object] = []
    outcomes: list[str] = []
    disposals: list[int] = []
    BACKEND_CLOSES.clear()
    resolve_backend = runtime_memory.resolve_memory_backend
    create_sink = runtime_memory.SQLiteMemoryAuditSink
    runtime_ctl = runtime_memory._runtime_artifactctl
    assembly_close = RuntimeMemoryAssembly.close

    def record_backend(**kwargs):
        resolved = resolve_backend(**kwargs)
        counter.count(type(resolved.store))
        stores.append(resolved.store)
        engine = getattr(resolved.store, "_engine", None)
        if engine is not None:
            from sqlalchemy import event

            event.listen(engine, "engine_disposed", lambda _e: disposals.append(1))
        return resolved

    def record_sink(db_path):
        sink = create_sink(db_path)
        sinks.append(sink)
        return sink

    def record_ctl(*args, **kwargs):
        ctl = runtime_ctl(*args, **kwargs)
        ctls.append(ctl)
        return ctl

    def recording_close(self):
        try:
            result = assembly_close(self)
            outcomes.append(str(result.reason_code))
            return result
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"raised {type(exc).__name__}: {exc}")
            raise

    counter.count(runtime_memory.SQLiteMemoryAuditSink)
    counter.count(ArtifactCtl)
    counter.patch(runtime_memory, "resolve_memory_backend", record_backend)
    counter.patch(runtime_memory, "SQLiteMemoryAuditSink", record_sink)
    counter.patch(runtime_memory, "_runtime_artifactctl", record_ctl)
    counter.patch(RuntimeMemoryAssembly, "close", recording_close)
    row: dict[str, object] = {}
    runtime = None
    try:
        try:
            runtime = APIRuntime.from_manager(runtime_manager(root, memory_config))
        except Exception as exc:  # noqa: BLE001
            row["build"] = f"{type(exc).__name__}: {exc}"
            row["stores_created"] = [type(s).__name__ for s in stores]
            return row
        row["build"] = "ok"
        [assembly] = list(runtime._memory_assemblies.values())
        row["memory_service_store"] = type(assembly.service._store).__name__ if assembly.service else None
        row["stores_created_by_resolve_memory_backend"] = [type(s).__name__ for s in stores]
        row["audit_sinks_created"] = len(sinks)
        if pg is not None:
            row["server_connections_after_build"] = pg.connections()
        if turn:
            try:
                with Agent(runtime=runtime) as agent:
                    row["turn"] = agent.run("hello", session_id="probe-388").run_state
            except Exception as exc:  # noqa: BLE001
                row["turn"] = f"raised {type(exc).__name__}: {exc}"
            if pg is not None:
                row["server_connections_after_turn"] = pg.connections()
        ctl = assembly.artifactctl
        per_close = []
        for _ in range(closes):
            runtime.close()
            per_close.append({
                "assembly_close_calls": len(outcomes),
                "created_store_closes": [counter.times(s) for s in stores],
                "audit_sink_closes": [counter.times(s) for s in sinks],
                "external_backend_closes": len(BACKEND_CLOSES),
                "assembly_artifact_store_closes": counter.times(ctl) if ctl is not None else None,
            })
            if pg is not None:
                per_close[-1]["engine_disposals"] = len(disposals)
        row["after_each_runtime_close"] = per_close
        row["assembly_close_outcomes"] = list(outcomes)
        row["assembly_artifact_store_is_runtime_artifactctl"] = ctl is not None and any(ctl is x for x in ctls)
        row["artifact_store_write_after_shutdown"] = (
            write_outcome(lambda: ctl.ingest_bytes(b"after shutdown")) if ctl is not None else "no artifact store")
        if ctl is not None:
            index = Path(ctl.config.index.sqlite_path)
            row["artifact_index_rename_delete_after_shutdown"] = rename_delete(index) if index.exists() else "missing"
        if pg is not None:
            row["server_connections_after_close"] = pg.settled()
            row["tables_in_run_schema"] = pg.tables()
        return row
    finally:
        counter.undo()
        if runtime is not None:
            for asm in runtime._memory_assemblies.values():
                if asm.artifactctl is not None and counter.times(asm.artifactctl) == 0:
                    try:
                        asm.artifactctl.close()
                    except Exception:  # noqa: BLE001
                        pass
        for store in stores:
            engine = getattr(store, "_engine", None)
            if engine is not None:
                engine.dispose()


def sqlite_store(name: str) -> dict:
    return {"backend": "sqlite", "sqlite_path": str(WORK / f"{name}-memory.db")}


# ---------------------------------------------------------------- A. owned close-capable backend
a = run_runtime(WORK / "A", external_config(WORK / "A-external.sqlite3", CLOSING_ADAPTER, sqlite_store("A")))
observed["A_external_owned_backend_with_close"] = a
closes_a = a.get("after_each_runtime_close", [])
c.check("A. external provider builds a real runtime whose memory service holds a BackendMemoryStoreAdapter; "
        "no memory.store store and no audit sink are created",
        a.get("build") == "ok" and a.get("memory_service_store") == "BackendMemoryStoreAdapter"
        and a.get("stores_created_by_resolve_memory_backend") == [] and a.get("audit_sinks_created") == 0,
        {k: a.get(k) for k in ("build", "memory_service_store", "stores_created_by_resolve_memory_backend",
                               "audit_sinks_created")})
c.check("A. one real echo turn completes", a.get("turn") == "completed", a.get("turn"))
c.check("A. runtime.close() x3: the assembly closes without error; the owned backend closes once and the "
        "assembly's artifact store closes once",
        a.get("assembly_close_outcomes") == ["closed"]
        and [x["external_backend_closes"] for x in closes_a] == [1, 1, 1]
        and [x["assembly_artifact_store_closes"] for x in closes_a] == [1, 1, 1],
        {"outcomes": a.get("assembly_close_outcomes"), "after_each_runtime_close": closes_a})
c.check("A. the artifact store rejects a write after shutdown, and its index file can be renamed and deleted "
        "right away (GC disabled, no collection)",
        str(a.get("artifact_store_write_after_shutdown", "")).startswith("ProgrammingError")
        and a.get("artifact_index_rename_delete_after_shutdown") == "ok",
        {"write": a.get("artifact_store_write_after_shutdown"),
         "rename_delete": a.get("artifact_index_rename_delete_after_shutdown")})

# ---------------------------------------------------------------- B. resource-free / no-close backend
b = run_runtime(WORK / "B", external_config(WORK / "B-external.sqlite3", "reference-sqlite", sqlite_store("B")))
observed["B_external_reference_sqlite_no_close"] = b
closes_b = b.get("after_each_runtime_close", [])
c.check("B. reference-sqlite (no close()): runtime builds with no memory.store store or audit sink, one turn "
        "completes, runtime.close() x3 closes the assembly without error and its artifact store once",
        b.get("build") == "ok" and b.get("stores_created_by_resolve_memory_backend") == []
        and b.get("audit_sinks_created") == 0 and b.get("turn") == "completed"
        and b.get("assembly_close_outcomes") == ["closed"]
        and [x["assembly_artifact_store_closes"] for x in closes_b] == [1, 1, 1]
        and str(b.get("artifact_store_write_after_shutdown", "")).startswith("ProgrammingError"),
        {k: b.get(k) for k in ("build", "stores_created_by_resolve_memory_backend", "audit_sinks_created", "turn",
                               "assembly_close_outcomes", "after_each_runtime_close",
                               "artifact_store_write_after_shutdown")})
b2 = run_runtime(WORK / "B2", external_config(WORK / "B2-external.sqlite3", "reference-sqlite",
                                              {"backend": "postgres"}), closes=2, turn=False)
observed["B2_external_with_unusable_memory_store"] = b2
c.check("B2. external provider with an unusable memory.store (postgres without URL): the runtime builds, "
        "creates no store, and closes cleanly",
        b2.get("build") == "ok" and b2.get("stores_created_by_resolve_memory_backend") == []
        and b2.get("assembly_close_outcomes") == ["closed"],
        {k: b2.get(k) for k in ("build", "stores_created_by_resolve_memory_backend", "assembly_close_outcomes")})

# ---------------------------------------------------------------- C. borrowed / owned backend ownership
closing_cls, reference_cls = BACKENDS["closing"], BACKENDS["reference"]
rows_c: dict[str, object] = {}


def ownership_case(label, make_closer, backend):
    BACKEND_CLOSES.clear()
    try:
        closer = make_closer(backend)
        closer.close()
        closer.close()
        result = {"backend_closes": BACKEND_CLOSES.count(id(backend))}
    except Exception as exc:  # noqa: BLE001
        result = {"raised": f"{type(exc).__name__}: {exc}"}

    def usable():
        adapter = BackendMemoryStoreAdapter(backend)
        rid = adapter.put(record(f"{label}-after-close"))
        assert adapter.get(rid) is not None

    result["backend_usable_after"] = write_outcome(usable)
    rows_c[label] = result
    return result


c1 = ownership_case("MemoryService(backend=b) [borrowed]", lambda b: MemoryService(backend=b),
                    closing_cls(WORK / "C1.sqlite3"))
c2 = ownership_case("BackendMemoryStoreAdapter(b) [borrowed]", lambda b: BackendMemoryStoreAdapter(b),
                    closing_cls(WORK / "C2.sqlite3"))
c3 = ownership_case("BackendMemoryStoreAdapter(b, owns_backend=True)",
                    lambda b: BackendMemoryStoreAdapter(b, owns_backend=True), closing_cls(WORK / "C3.sqlite3"))
c4 = ownership_case("MemoryService(backend=b, owns_store=True)",
                    lambda b: MemoryService(backend=b, owns_store=True), closing_cls(WORK / "C4.sqlite3"))
c5 = ownership_case("MemoryService(backend=reference-no-close, owns_store=True)",
                    lambda b: MemoryService(backend=b, owns_store=True), reference_cls(WORK / "C5.sqlite3"))
observed["C_backend_ownership"] = rows_c
c.check("C. a borrowed backend (MemoryService(backend=b) / BackendMemoryStoreAdapter(b)) is not closed by two "
        "closes and stays usable",
        c1.get("backend_closes") == 0 and c2.get("backend_closes") == 0
        and c1["backend_usable_after"] == "accepted" and c2["backend_usable_after"] == "accepted",
        {"service": c1, "adapter": c2})
c.check("C. an owned backend (owns_backend=True / MemoryService(..., owns_store=True)) is closed once over two closes",
        c3.get("backend_closes") == 1 and c4.get("backend_closes") == 1, {"adapter": c3, "service": c4})
c.check("C. an owned backend without close() closes twice without error and stays usable",
        "raised" not in c5 and c5["backend_usable_after"] == "accepted", c5)

# ---------------------------------------------------------------- D. default built-in provider
d = run_runtime(WORK / "D", None)
observed["D_default_builtin_provider"] = d
closes_d = d.get("after_each_runtime_close", [])
c.check("D. default provider (control): one SQLiteMemoryStore and one audit sink; turn completes; runtime.close() "
        "x3 closes store, audit sink and artifact store once each",
        d.get("build") == "ok" and d.get("stores_created_by_resolve_memory_backend") == ["SQLiteMemoryStore"]
        and d.get("audit_sinks_created") == 1 and d.get("turn") == "completed"
        and d.get("assembly_close_outcomes") == ["closed"]
        and [(x["created_store_closes"], x["audit_sink_closes"], x["assembly_artifact_store_closes"])
             for x in closes_d] == [([1], [1], 1)] * 3,
        {k: d.get(k) for k in ("stores_created_by_resolve_memory_backend", "audit_sinks_created", "turn",
                               "assembly_close_outcomes", "after_each_runtime_close")})


# ---------------------------------------------------------------- E. scheduler boundary
class FakeArtifactStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("artifactctl.close")


def builder(memory_root: Path, memory_config, **overrides):
    config = OpenMinionConfig()
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"
    args = dict(config=config, agent_id="alpha", memory_root=memory_root, logger=logging.getLogger("probe_388"),
                config_manager=None, home_root=None, data_root=None, session_context=None, retrieve_ctl=None,
                storage_path=None)
    if memory_config is not None:
        args["resolve_runtime_memory_config_fn"] = lambda **kwargs: memory_config
    args.update(overrides)
    return runtime_memory.build_memory_v2_runtime_assembly(**args)


rows_e: dict[str, object] = {}
for label, cfg in (("external backend with close()", external_config(WORK / "E-ext.sqlite3", CLOSING_ADAPTER)),
                   ("built-in provider (SQLite store)", None)):
    events: list[str] = []
    BACKEND_CLOSES.clear()
    counter = CloseCounter()
    stores_e: list[object] = []
    resolve_backend = runtime_memory.resolve_memory_backend

    def record_backend(**kwargs):
        resolved = resolve_backend(**kwargs)
        counter.count(type(resolved.store))
        stores_e.append(resolved.store)
        return resolved

    counter.patch(runtime_memory, "resolve_memory_backend", record_backend)
    scheduler = Scheduler(alive=True)
    try:
        asm = builder(WORK / f"E-{len(rows_e)}", cfg, scheduler=scheduler,
                      artifactctl_factory=lambda events=events: FakeArtifactStore(events))
        blocked = [asm.close(), asm.close()]
        while_blocked = {"backend_closes": len(BACKEND_CLOSES), "store_closes": [counter.times(s) for s in stores_e],
                         "artifact_events": list(events)}
        usable = write_outcome(lambda: asm.service.write_record(scope="agent:alpha", record_type="fact",
                                                               title="while blocked", content="x"))
        scheduler.alive = False
        done = [asm.close(), asm.close()]
        rows_e[label] = {
            "blocked": [f"{r.reason_code} closed={r.closed}" for r in blocked],
            "while_blocked": while_blocked, "service_write_while_blocked": usable,
            "after_stop": [f"{r.reason_code} closed={r.closed}" for r in done],
            "same_result_object_after_stop": done[0] is done[1],
            "after_stop_closes": {"backend_closes": len(BACKEND_CLOSES),
                                  "store_closes": [counter.times(s) for s in stores_e],
                                  "artifact_events": list(events)},
            "scheduler_events": scheduler.events,
        }
    except Exception as exc:  # noqa: BLE001 - recorded (the 0.1.11 control raises here)
        rows_e[label] = {"raised": f"{type(exc).__name__}: {exc}", "blocked": [], "while_blocked": None,
                         "after_stop": [""], "after_stop_closes": None, "same_result_object_after_stop": False,
                         "service_write_while_blocked": None, "scheduler_events": scheduler.events}
    finally:
        counter.undo()
observed["E_scheduler_boundary"] = rows_e
ee, eb = rows_e["external backend with close()"], rows_e["built-in provider (SQLite store)"]
c.check("E. external backend, live scheduler: close() x2 is scheduler_still_running; backend and artifact store "
        "stay open, the service still writes; after the scheduler stops, close succeeds and closes each once",
        bool(ee["blocked"]) and all(x.startswith("scheduler_still_running closed=False") for x in ee["blocked"])
        and ee["while_blocked"] == {"backend_closes": 0, "store_closes": [], "artifact_events": []}
        and ee["service_write_while_blocked"] == "accepted"
        and ee["after_stop"][0].endswith("closed=True") and ee["same_result_object_after_stop"]
        and ee["after_stop_closes"] == {"backend_closes": 1, "store_closes": [], "artifact_events": ["artifactctl.close"]},
        ee)
c.check("E. built-in provider, live scheduler (boundary preserved): store and artifact store stay open while it "
        "runs; after it stops, each closes once",
        bool(eb["blocked"]) and all(x.startswith("scheduler_still_running closed=False") for x in eb["blocked"])
        and eb["while_blocked"] == {"backend_closes": 0, "store_closes": [0], "artifact_events": []}
        and eb["service_write_while_blocked"] == "accepted"
        and eb["after_stop"][0].endswith("closed=True") and eb["same_result_object_after_stop"]
        and eb["after_stop_closes"] == {"backend_closes": 0, "store_closes": [1], "artifact_events": ["artifactctl.close"]},
        eb)

# ---------------------------------------------------------------- P. PostgreSQL memory.store
if admin is not None:
    db = PgDatabase(admin, PG_URL, "388ext")
    try:
        p = run_runtime(WORK / "P", external_config(WORK / "P-external.sqlite3", "reference-sqlite",
                                                    {"backend": "postgres", "postgres": {"url": db.url}}),
                        closes=2, pg=db)
    finally:
        db.drop()
    observed["P_external_with_postgres_memory_store"] = p
    c.check("P. external provider + PostgreSQL memory.store: no memory store is created, 0 server connections "
            "after build, after one turn and after close, and no tables in the run's schema",
            p.get("build") == "ok" and p.get("stores_created_by_resolve_memory_backend") == []
            and p.get("turn") == "completed"
            and p.get("server_connections_after_build") == 0 and p.get("server_connections_after_turn") == 0
            and p.get("server_connections_after_close") == 0 and p.get("tables_in_run_schema") == 0
            and p.get("assembly_close_outcomes") == ["closed"],
            {k: p.get(k) for k in ("build", "stores_created_by_resolve_memory_backend", "turn",
                                   "server_connections_after_build", "server_connections_after_turn",
                                   "server_connections_after_close", "tables_in_run_schema",
                                   "assembly_close_outcomes")})
    db = PgDatabase(admin, PG_URL, "388def")
    try:
        q = run_runtime(WORK / "Q", {"store": {"backend": "postgres", "postgres": {"url": db.url}}}, closes=2, pg=db)
    finally:
        db.drop()
    observed["P_control_default_provider_with_postgres_memory_store"] = q
    closes_q = q.get("after_each_runtime_close", [])
    c.check("P. positive control: the default provider with the same PostgreSQL memory.store opens one engine "
            "(server connections > 0 after the turn), disposes it once over two closes, leaves 0 connections",
            q.get("stores_created_by_resolve_memory_backend") == ["PostgresMemoryStore"]
            and (q.get("server_connections_after_turn") or 0) >= 1
            and [x.get("engine_disposals") for x in closes_q] == [1, 1]
            and q.get("server_connections_after_close") == 0,
            {k: q.get(k) for k in ("stores_created_by_resolve_memory_backend", "turn", "server_connections_after_build",
                                   "server_connections_after_turn", "after_each_runtime_close",
                                   "server_connections_after_close", "tables_in_run_schema")})
    admin.dispose()

info["gc_at_end"] = GC.state()
c.check("GC stayed disabled and gc.collect() was never called", GC.state() == {"gc_enabled": False,
                                                                                 "gc_collect_calls": 0}, GC.state())
write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": observed})
s = c.summary()
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
sys.exit(0 if c.all_pass else 1)
