"""#389 on the installed package: the bootstrap memory fallback closes with its owner.

Cyclic GC is disabled before openminion is imported; gc.collect() is counted
and never called. Every owner is closed twice. Counts are close calls per
object created by the memory assembly behind the fallback:
[memory store, audit sink, artifact store] (the store and sink exist only for
the built-in provider; the external backend is counted separately).

  A. SQLite memory store (default memory config):
     A1 direct `build_agent_memory_service` caller, after one record_turn;
     A2 gateway service that `build_gateway_service` builds without agent_memory,
        after one turn through GatewayService.run_once (a stub agent replies);
     A3 borrowed: a gateway service given the fallback memory as agent_memory
        closes nothing; then the memory's own close() closes everything once;
     A4 SDK control: the runtime's memory gateway and gateway service close
        nothing themselves (the runtime owns them); runtime.close() closes once.
  B. External backend through the fallback (#398 behaviour preserved): with a
     backend that has close() and with the reference backend without close():
     direct caller, gateway-built fallback, and a borrowed adapter.
  C. Scheduler shutdown boundary: with a live scheduler the owner's close()
     closes nothing; after it stops, two closes close everything once
     (gateway owner and direct caller; built-in store and external backend).
  P. (only with OPENMINION_TEST_POSTGRES_URL) PostgreSQL memory store: direct
     caller, gateway-built fallback and borrowed adapter; engine disposals and
     server connections for the run's application_name.

Usage: python -I -X utf8 probe_389.py <work_dir> <out.json>
"""

from __future__ import annotations

import gc

gc.disable()

import asyncio  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

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
    fallback_config,
    fallback_manager,
    register_backends,
    rename_delete,
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
    extra["postgres"] = "not configured: the PostgreSQL cases (P) are not run here"
info = banner("probe_389", **extra)

from openminion import Agent, APIRuntime  # noqa: E402
from openminion.base.channel import ChannelRegistry  # noqa: E402
from openminion.modules.artifact.control import ArtifactCtl  # noqa: E402
from openminion.modules.storage.runtime.idempotency_store import IdempotencyStore  # noqa: E402
from openminion.modules.storage.runtime.migrations import migrate_database  # noqa: E402
from openminion.modules.storage.runtime.session_store import SessionStore  # noqa: E402
from openminion.modules.storage.runtime.sqlite import connect_database  # noqa: E402
from openminion.services.runtime import bootstrap  # noqa: E402
from openminion.services.runtime import memory as runtime_memory  # noqa: E402

register_backends()
os.environ["OPENMINION_HOME"] = str(WORK / "env-home")
os.environ["OPENMINION_DATA_ROOT"] = str(WORK / "env-home" / "data")

c = Checks()
observed: dict[str, object] = {}
TURN = {"session_id": "s1", "run_id": "r1", "request_id": "q1", "channel": "console", "target": "probe",
        "user_message": "Remember: the probe colour is green.", "assistant_message": "Noted."}


class ReplyAgent:
    async def run_turn(self, message, history=None, **_kwargs):
        return SimpleNamespace(text="Noted.", channel=message.channel, target=message.target, metadata={})


class Recorder:
    """Records the assemblies, stores and audit sinks the fallback creates and counts their closes."""

    def __init__(self, scheduler: Scheduler | None = None) -> None:
        self.counter = CloseCounter()
        self.assemblies: list[object] = []
        self.stores: list[object] = []
        self.sinks: list[object] = []
        self.disposals: list[int] = []
        self.scheduler = scheduler
        build_assembly = runtime_memory.build_memory_v2_runtime_assembly
        resolve_backend = runtime_memory.resolve_memory_backend
        create_sink = runtime_memory.SQLiteMemoryAuditSink

        def record_assembly(**kwargs):
            if self.scheduler is not None:
                kwargs["scheduler"] = self.scheduler
            assembly = build_assembly(**kwargs)
            self.assemblies.append(assembly)
            return assembly

        def record_backend(**kwargs):
            resolved = resolve_backend(**kwargs)
            self.counter.count(type(resolved.store))
            self.stores.append(resolved.store)
            engine = getattr(resolved.store, "_engine", None)
            if engine is not None:
                from sqlalchemy import event

                event.listen(engine, "engine_disposed", lambda _e: self.disposals.append(1))
            return resolved

        def record_sink(db_path):
            sink = create_sink(db_path)
            self.sinks.append(sink)
            return sink

        self.counter.count(runtime_memory.SQLiteMemoryAuditSink)
        self.counter.count(ArtifactCtl)
        self.counter.patch(runtime_memory, "build_memory_v2_runtime_assembly", record_assembly)
        self.counter.patch(runtime_memory, "resolve_memory_backend", record_backend)
        self.counter.patch(runtime_memory, "SQLiteMemoryAuditSink", record_sink)

    def assembly(self):
        [assembly] = self.assemblies
        return assembly

    def counts(self) -> dict[str, object]:
        ctl = self.assembly().artifactctl if self.assemblies else None
        return {"store": [self.counter.times(s) for s in self.stores],
                "audit_sink": [self.counter.times(s) for s in self.sinks],
                "artifact_store": self.counter.times(ctl) if ctl is not None else None,
                "external_backend": len(BACKEND_CLOSES),
                "engine_disposals": len(self.disposals)}

    def uninstall(self) -> None:
        self.counter.undo()
        for assembly in self.assemblies:
            for item in (*self.sinks, getattr(assembly, "artifactctl", None)):
                if item is not None and self.counter.times(item) == 0:
                    try:
                        item.close()
                    except Exception:  # noqa: BLE001
                        pass
        for store in self.stores:
            engine = getattr(store, "_engine", None)
            if engine is not None:
                engine.dispose()


def fallback_kwargs(root: Path, config, manager) -> dict:
    return {"config": config, "memory_root": root / "memory", "home_root": root, "data_root": root / "data",
            "config_manager": manager, "logger": logging.getLogger("probe_389")}


def build_memory(root: Path, config, manager):
    return bootstrap.build_agent_memory_service(agent_id="main", storage_path=root / "state" / "openminion.db",
                                                **fallback_kwargs(root, config, manager))


def build_gateway(root: Path, config, manager, connection, agent_memory=None):
    return bootstrap.build_gateway_service(
        agent_service=ReplyAgent(), profile_name="main", channels=ChannelRegistry([]),
        sessions=SessionStore(connection), idempotency=IdempotencyStore(connection), security_policy=None,
        channel_authenticity_policy=None, config_path=root / "agents.json",
        storage_path=root / "state" / "openminion.db", agent_memory=agent_memory,
        **fallback_kwargs(root, config, manager))


def gateway_turn(gateway) -> str:
    try:
        response = asyncio.run(gateway.run_once(channel="console", target="probe", message=TURN["user_message"],
                                                session_id="s1", deliver=False))
        return str(response.body)
    except Exception as exc:  # noqa: BLE001
        return f"raised {type(exc).__name__}: {exc}"


def close_twice(rec: Recorder, closer) -> object:
    close = getattr(closer, "close", None)
    if not callable(close):
        return f"{type(closer).__name__} has no close()"
    out = []
    for _ in range(2):
        try:
            close()
            out.append(rec.counts())
        except Exception as exc:  # noqa: BLE001
            out.append(f"raised {type(exc).__name__}: {exc}")
    return out


def run_owner(root: Path, owner: str, memory_config=None, pg: PgDatabase | None = None,
              scheduler: Scheduler | None = None) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    BACKEND_CLOSES.clear()
    config = fallback_config("main")
    manager = fallback_manager(root, config, memory_config)
    database = root / "state" / "openminion.db"
    migrate_database(database)
    connection = connect_database(database)
    rec = Recorder(scheduler)
    row: dict[str, object] = {"owner": owner}
    try:
        if owner == "caller":
            closer = build_memory(root, config, manager)
            row["closer"] = type(closer).__name__
            row["record_turn"] = write_outcome(lambda: closer.record_turn(**TURN))
        elif owner == "gateway":
            closer = build_gateway(root, config, manager, connection)
            row["closer"] = type(closer).__name__
            row["turn_reply"] = gateway_turn(closer)
        else:  # borrowed
            memory = build_memory(root, config, manager)
            closer = build_gateway(root, config, manager, connection, memory)
            row["closer"] = type(closer).__name__
            row["turn_reply"] = gateway_turn(closer)
        row["assemblies_built"] = len(rec.assemblies)
        row["stores_created"] = [type(s).__name__ for s in rec.stores]
        row["audit_sinks_open_after_turn"] = [s._conn is not None for s in rec.sinks]
        if pg is not None:
            row["server_connections_after_turn"] = pg.connections()
        if scheduler is not None:
            first = getattr(closer, "close", lambda: None)
            first_outcome = write_outcome(first)
            row["close_while_scheduler_alive"] = {"outcome": first_outcome, "counts": rec.counts()}
            if pg is not None:
                row["server_connections_while_scheduler_alive"] = pg.connections()
            scheduler.alive = False
        row["after_each_close"] = close_twice(rec, closer)
        if owner == "borrowed":
            row["audit_sinks_open_after_gateway_close"] = [s._conn is not None for s in rec.sinks]
            if pg is not None:
                row["server_connections_after_gateway_close"] = pg.connections()
            row["then_the_memory_own_close"] = close_twice(rec, memory)
        row["audit_sinks_open_after_close"] = [s._conn is not None for s in rec.sinks]
        ctl = rec.assembly().artifactctl if rec.assemblies else None
        row["artifact_store_write_after_close"] = (
            write_outcome(lambda: ctl.ingest_bytes(b"after close")) if ctl is not None else "no artifact store")
        if pg is not None:
            row["server_connections_after_close"] = pg.settled()
        elif ctl is not None:
            files = {"artifact_index": Path(ctl.config.index.sqlite_path)}
            for i, sink in enumerate(rec.sinks):
                path = getattr(sink, "_db_path", None) or getattr(sink, "db_path", None)
                if path is not None:
                    files[f"audit_sink_{i}"] = Path(path)
            row["rename_delete_after_close"] = {k: (rename_delete(v) if v.exists() else "missing")
                                                for k, v in files.items()}
        return row
    finally:
        rec.uninstall()
        connection.close()


def counts_seq(row: dict, key: str = "after_each_close") -> list:
    return [x if isinstance(x, str) else (x["store"], x["audit_sink"], x["artifact_store"])
            for x in (row.get(key) if isinstance(row.get(key), list) else [row.get(key)])]


# ---------------------------------------------------------------- A. SQLite (built-in)
rows_a = {o: run_owner(WORK / f"A-{o}", o) for o in ("caller", "gateway", "borrowed")}
observed["A_sqlite"] = rows_a
for o, label in (("caller", "A1. direct build_agent_memory_service caller"),
                 ("gateway", "A2. gateway service built without agent_memory")):
    r = rows_a[o]
    c.check(f"{label}: one SQLiteMemoryStore; close() x2 closes store, audit sink and artifact store once each; "
            "sink closed and artifact store rejects writes afterwards",
            r["stores_created"] == ["SQLiteMemoryStore"] and r["audit_sinks_open_after_turn"] == [True]
            and counts_seq(r) == [([1], [1], 1), ([1], [1], 1)] and r["audit_sinks_open_after_close"] == [False]
            and str(r["artifact_store_write_after_close"]).startswith("ProgrammingError")
            and r.get("record_turn", "accepted") == "accepted" and r.get("turn_reply", "Noted.") == "Noted.",
            {k: r.get(k) for k in ("closer", "record_turn", "turn_reply", "stores_created", "audit_sinks_open_after_turn",
                                   "after_each_close", "audit_sinks_open_after_close",
                                   "artifact_store_write_after_close")})
    c.check(f"{label}: right after close, the fallback's artifact index and audit DB files can be renamed and "
            "deleted (GC disabled, no collection)",
            r.get("rename_delete_after_close") and all(v == "ok" for v in r["rename_delete_after_close"].values()),
            r.get("rename_delete_after_close"))
r = rows_a["borrowed"]
c.check("A3. borrowed: a gateway service given the fallback memory as agent_memory closes nothing on two closes "
        "(sink stays open); the memory's own close() x2 then closes each once",
        r["assemblies_built"] == 1 and counts_seq(r) == [([0], [0], 0), ([0], [0], 0)]
        and r["audit_sinks_open_after_gateway_close"] == [True]
        and counts_seq(r, "then_the_memory_own_close") == [([1], [1], 1), ([1], [1], 1)]
        and r["audit_sinks_open_after_close"] == [False],
        {k: r.get(k) for k in ("assemblies_built", "turn_reply", "after_each_close",
                               "audit_sinks_open_after_gateway_close", "then_the_memory_own_close",
                               "audit_sinks_open_after_close")})


# A4. SDK control
def run_sdk(root: Path) -> dict:
    from _common import echo_config

    path = echo_config(root, memory_enabled=True, memory_provider="memory_v2")
    rec = Recorder()
    runtime = None
    row: dict[str, object] = {}
    try:
        runtime = APIRuntime.from_config_path(str(path), home_root=root, data_root=root / ".openminion")
        with Agent(runtime=runtime) as agent:
            row["turn"] = agent.run("hello").run_state
        gateway = rec.assembly().gateway
        row["runtime_gateway_uses_this_memory_gateway"] = runtime.gateway._agent_memory is gateway
        row["memory_gateway_close_owner_is_none"] = getattr(gateway, "close_owner", None) is None
        getattr(gateway, "close", lambda: None)()
        runtime.gateway.close()
        row["after_memory_gateway_and_gateway_service_close"] = rec.counts()
        row["after_each_runtime_close"] = []
        for _ in range(2):
            runtime.close()
            row["after_each_runtime_close"].append(rec.counts())
        return row
    finally:
        if runtime is not None:
            runtime.close()
        rec.uninstall()


sdk = run_sdk(WORK / "A4-sdk")
observed["A4_sdk_control"] = sdk
c.check("A4. SDK control: the runtime's memory gateway (close_owner None) and gateway service close nothing "
        "themselves; runtime.close() x2 closes store, sink and artifact store once each",
        sdk["turn"] == "completed" and sdk["runtime_gateway_uses_this_memory_gateway"]
        and sdk["memory_gateway_close_owner_is_none"]
        and counts_seq(sdk, "after_memory_gateway_and_gateway_service_close") == [([0], [0], 0)]
        and counts_seq(sdk, "after_each_runtime_close") == [([1], [1], 1), ([1], [1], 1)], sdk)

# ---------------------------------------------------------------- B. external backend
rows_b: dict[str, dict] = {}
for adapter, kind in ((CLOSING_ADAPTER, "with close()"), ("reference-sqlite", "without close()")):
    for o in ("caller", "gateway", "borrowed"):
        key = f"{o}, external backend {kind}"
        rows_b[key] = run_owner(WORK / f"B-{len(rows_b)}", o,
                                external_config(WORK / f"B-{len(rows_b)}-ext.sqlite3", adapter))
observed["B_external_backend"] = rows_b


def ext_seq(row: dict, key: str = "after_each_close") -> list:
    return [x if isinstance(x, str) else (x["store"], x["audit_sink"], x["artifact_store"], x["external_backend"])
            for x in (row.get(key) if isinstance(row.get(key), list) else [row.get(key)])]


for o, label in (("caller", "direct caller"), ("gateway", "gateway-built fallback")):
    r1, r2 = rows_b[f"{o}, external backend with close()"], rows_b[f"{o}, external backend without close()"]
    c.check(f"B. external backend, {label}: no memory store or audit sink is created; close() x2 closes the "
            "owned backend once and the artifact store once; a backend without close() closes cleanly",
            r1["stores_created"] == [] and ext_seq(r1) == [([], [], 1, 1), ([], [], 1, 1)]
            and r2["stores_created"] == [] and ext_seq(r2) == [([], [], 1, 0), ([], [], 1, 0)]
            and r1.get("record_turn", "accepted") == "accepted" and r1.get("turn_reply", "Noted.") == "Noted."
            and r2.get("record_turn", "accepted") == "accepted" and r2.get("turn_reply", "Noted.") == "Noted.",
            {"with_close": {k: r1.get(k) for k in ("record_turn", "turn_reply", "stores_created", "after_each_close")},
             "without_close": {k: r2.get(k) for k in ("record_turn", "turn_reply", "stores_created",
                                                       "after_each_close")}})
r1, r2 = rows_b["borrowed, external backend with close()"], rows_b["borrowed, external backend without close()"]
c.check("B. external backend, borrowed adapter: the gateway service given it closes nothing (backend and artifact "
        "store stay open); the adapter's own close() x2 then closes each once",
        ext_seq(r1) == [([], [], 0, 0), ([], [], 0, 0)]
        and ext_seq(r1, "then_the_memory_own_close") == [([], [], 1, 1), ([], [], 1, 1)]
        and ext_seq(r2) == [([], [], 0, 0), ([], [], 0, 0)]
        and ext_seq(r2, "then_the_memory_own_close") == [([], [], 1, 0), ([], [], 1, 0)],
        {"with_close": {k: r1.get(k) for k in ("after_each_close", "then_the_memory_own_close")},
         "without_close": {k: r2.get(k) for k in ("after_each_close", "then_the_memory_own_close")}})

# ---------------------------------------------------------------- C. scheduler boundary
rows_c: dict[str, dict] = {}
for o in ("gateway", "caller"):
    rows_c[f"{o}, built-in store"] = run_owner(WORK / f"C-{o}", o, scheduler=Scheduler(alive=True))
rows_c["caller, external backend with close()"] = run_owner(
    WORK / "C-ext", "caller", external_config(WORK / "C-ext.sqlite3", CLOSING_ADAPTER), scheduler=Scheduler(alive=True))
observed["C_scheduler_boundary"] = rows_c
for o in ("gateway", "caller"):
    r = rows_c[f"{o}, built-in store"]
    blocked = r["close_while_scheduler_alive"]["counts"]
    c.check(f"C. {o} owner, live scheduler: close() closes nothing; after the scheduler stops, close() x2 closes "
            "store, sink and artifact store once each",
            (blocked["store"], blocked["audit_sink"], blocked["artifact_store"]) == ([0], [0], 0)
            and counts_seq(r) == [([1], [1], 1), ([1], [1], 1)],
            {"while_alive": r["close_while_scheduler_alive"], "after_stop": r["after_each_close"]})
r = rows_c["caller, external backend with close()"]
blocked = r["close_while_scheduler_alive"]["counts"]
c.check("C. direct caller, external backend, live scheduler: backend and artifact store stay open; after it stops, "
        "close() x2 closes each once",
        (blocked["artifact_store"], blocked["external_backend"]) == (0, 0)
        and ext_seq(r) == [([], [], 1, 1), ([], [], 1, 1)],
        {"while_alive": r["close_while_scheduler_alive"], "after_stop": r["after_each_close"]})

# ---------------------------------------------------------------- P. PostgreSQL
if admin is not None:
    rows_p: dict[str, dict] = {}
    for o in ("caller", "gateway", "borrowed"):
        db = PgDatabase(admin, PG_URL, f"389{o[:3]}")
        try:
            rows_p[o] = run_owner(WORK / f"P-{o}", o, {"store": {"backend": "postgres", "postgres": {"url": db.url}}},
                                  pg=db)
        finally:
            db.drop()
    observed["P_postgres"] = rows_p
    for o, label in (("caller", "direct caller"), ("gateway", "gateway-built fallback")):
        r = rows_p[o]
        seq = r["after_each_close"]
        c.check(f"P. PostgreSQL, {label}: one PostgresMemoryStore with server connections after the turn; close() x2 "
                "disposes the engine once and closes store, sink and artifact store once each; 0 connections after",
                r["stores_created"] == ["PostgresMemoryStore"] and r["server_connections_after_turn"] >= 1
                and isinstance(seq, list) and [x["engine_disposals"] for x in seq] == [1, 1]
                and counts_seq(r) == [([1], [1], 1), ([1], [1], 1)] and r["server_connections_after_close"] == 0
                and r.get("record_turn", "accepted") == "accepted" and r.get("turn_reply", "Noted.") == "Noted.",
                {k: r.get(k) for k in ("record_turn", "turn_reply", "stores_created", "server_connections_after_turn",
                                       "after_each_close", "server_connections_after_close")})
    r = rows_p["borrowed"]
    seq, own = r["after_each_close"], r["then_the_memory_own_close"]
    c.check("P. PostgreSQL, borrowed adapter: the gateway service's two closes dispose nothing and the server "
            "connection stays; the adapter's own close() x2 disposes once and leaves 0 connections",
            isinstance(seq, list) and [x["engine_disposals"] for x in seq] == [0, 0]
            and r["server_connections_after_gateway_close"] >= 1
            and isinstance(own, list) and [x["engine_disposals"] for x in own] == [1, 1]
            and counts_seq(r, "then_the_memory_own_close") == [([1], [1], 1), ([1], [1], 1)]
            and r["server_connections_after_close"] == 0,
            {k: r.get(k) for k in ("after_each_close", "server_connections_after_gateway_close",
                                   "then_the_memory_own_close", "server_connections_after_close")})
    admin.dispose()

info["gc_at_end"] = GC.state()
c.check("GC stayed disabled and gc.collect() was never called",
        GC.state() == {"gc_enabled": False, "gc_collect_calls": 0}, GC.state())
write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": observed})
s = c.summary()
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
sys.exit(0 if c.all_pass else 1)
