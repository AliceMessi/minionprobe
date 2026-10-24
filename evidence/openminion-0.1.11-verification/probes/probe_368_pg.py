"""#368 on a live PostgreSQL server: the runtime disposes the engine its memory store owns.

  A. SDK: an echo runtime whose memory store is PostgreSQL (the memory config is
     registered on the ConfigManager, as upstream's integration test does) runs one
     turn; the server connections for this runtime's application_name are counted
     in pg_stat_activity; runtime.close() three times; engine disposals counted
     (SQLAlchemy engine_disposed) with the openminion frames that dispose.
  B. Borrowed audited store: PostgresMemoryStore(owns_engine=True) wrapped in
     AuditedMemoryStore(store) without owns_store; two wrapper closes keep the
     engine and its server connection, and the store stays usable; then the owner
     closes the store twice: one disposal, no connections left.
  C. scheduler_still_running with the PostgreSQL store (installed builder, since
     APIRuntime cannot inject a scheduler): the engine is not disposed while the
     scheduler is alive and the store stays usable; after it stops, one disposal.

Needs OPENMINION_TEST_POSTGRES_URL and openminion[postgres]. Every case uses its
own schema and application_name. The URL is never printed.

Usage: python -I probe_368_pg.py <work_dir> <out.json>
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, echo_config, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()
BASE_URL = os.environ["OPENMINION_TEST_POSTGRES_URL"]

import sqlalchemy as sa  # noqa: E402
from sqlalchemy import event  # noqa: E402

admin = sa.create_engine(BASE_URL, future=True)
with admin.connect() as _conn:
    server_version = _conn.execute(sa.text("SHOW server_version")).scalar_one()
import psycopg  # noqa: E402

info = banner("probe_368_pg", postgres_server_version=server_version, sqlalchemy=sa.__version__,
              psycopg=psycopg.__version__)

from openminion import Agent, APIRuntime  # noqa: E402
from openminion.base.config import ConfigManager, OpenMinionConfig  # noqa: E402
from openminion.modules.memory.config import from_base_config  # noqa: E402
from openminion.modules.memory.models import MemoryRecord  # noqa: E402
from openminion.modules.memory.storage.audit import AuditedMemoryStore  # noqa: E402
from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore  # noqa: E402
from openminion.services.runtime import memory as runtime_memory  # noqa: E402

c = Checks()
observed: dict[str, object] = {}


class Database:
    def __init__(self, label: str) -> None:
        token = uuid.uuid4().hex[:10]
        self.schema = f"probe368_{label}_{token}"
        self.app = f"probe368_{label}_{token}"
        with admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{self.schema}"'))
        self.url = f"{BASE_URL}?options={quote_plus('-csearch_path=' + self.schema)}&application_name={self.app}"

    def connections(self) -> int:
        with admin.connect() as conn:
            return conn.execute(sa.text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :a"),
                                {"a": self.app}).scalar_one()

    def settled(self) -> int:
        deadline = time.monotonic() + 5.0
        count = self.connections()
        while count and time.monotonic() < deadline:
            time.sleep(0.05)
            count = self.connections()
        return count

    def drop(self) -> None:
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))


def openminion_frames() -> list[str]:
    out = []
    for frame in traceback.extract_stack()[:-2]:
        path = frame.filename.replace("\\", "/")
        if "/site-packages/openminion/" in path:
            out.append(f"openminion/{path.split('/site-packages/openminion/', 1)[1]}:{frame.lineno} {frame.name}")
    return out[-6:]


def record(rid: str) -> MemoryRecord:
    return MemoryRecord(id=rid, scope="session:s1", type="fact", title=rid, content={"text": rid},
                        created_at="2026-10-24T00:00:00+00:00", updated_at="2026-10-24T00:00:00+00:00")


def pg_memory_config(url: str):
    def fn(**kwargs):
        memory_config = from_base_config(**kwargs)
        store = dataclasses.replace(memory_config.store, backend="postgres", postgres_url=url)
        return dataclasses.replace(memory_config, store=store)

    return fn


# ---------------------------------------------------------------- A. SDK
db = Database("sdk")
stores: list[PostgresMemoryStore] = []
disposals: list[list[str]] = []
resolve_backend = runtime_memory.resolve_memory_backend


def record_backend(**kwargs):
    resolved = resolve_backend(**kwargs)
    stores.append(resolved.store)
    event.listen(resolved.store._engine, "engine_disposed", lambda engine: disposals.append(openminion_frames()))
    return resolved


runtime_memory.resolve_memory_backend = record_backend
try:
    root = WORK / "sdk-root"
    path = echo_config(root)
    manager = ConfigManager.load(str(path), home_root=root, data_root=root / ".openminion")
    manager.register("memory", pg_memory_config(db.url))
    runtime = APIRuntime.from_manager(manager)
    try:
        with Agent(runtime=runtime) as agent:
            run_state = agent.run("hello").run_state
        before = db.connections()
        tables = None
        with admin.connect() as conn:
            tables = conn.execute(sa.text("SELECT count(*) FROM information_schema.tables WHERE table_schema = :s"),
                                  {"s": db.schema}).scalar_one()
        counts = []
        for _ in range(3):
            runtime.close()
            counts.append(len(disposals))
        after = db.settled()
    finally:
        runtime.close()
        for s in stores:
            s._engine.dispose()
finally:
    runtime_memory.resolve_memory_backend = resolve_backend
    db.drop()
observed["A_sdk_postgres"] = {
    "run_state": run_state, "memory_stores": [type(s).__name__ for s in stores],
    "memory_tables_in_schema": tables, "server_connections_after_turn": before,
    "engine_disposals_after_each_runtime_close": counts, "server_connections_after_close": after,
    "dispose_stack": disposals[:1],
}
c.check("A. echo SDK turn completes with one PostgresMemoryStore from resolve_memory_backend",
        run_state == "completed" and [type(s).__name__ for s in stores] == ["PostgresMemoryStore"],
        {"run_state": run_state, "stores": [type(s).__name__ for s in stores]})
c.check("A. the turn opened an actual server connection for this runtime's application_name (memory tables created)",
        before >= 1 and (tables or 0) > 0, {"connections": before, "tables": tables})
c.check("A. runtime.close() x3 disposes the owned engine exactly once", counts == [1, 1, 1], counts)
c.check("A. no server connection is left for the application_name after close", after == 0, after)

# ---------------------------------------------------------------- B. borrowed audited store
db = Database("borrowed")
engine = sa.create_engine(db.url, future=True)
disposed: list[object] = []
event.listen(engine, "engine_disposed", disposed.append)
try:
    store = PostgresMemoryStore(engine, database_path=WORK / "borrowed" / "memory.db", artifactctl=None, owns_engine=True)
    audited = AuditedMemoryStore(store)
    rid = audited.put(record("borrowed-r1"))
    audited.close()
    audited.close()
    after_wrapper = {"disposals": len(disposed), "connections": db.connections(),
                     "store_get": store.get(rid) is not None}
    rid2 = store.put(record("borrowed-r2"))
    after_wrapper["store_put_and_get_after_wrapper_close"] = store.get(rid2) is not None
    store.close()
    store.close()
    after_owner = {"disposals": len(disposed), "connections": db.settled()}
finally:
    engine.dispose()
    db.drop()
observed["B_borrowed_audited_postgres"] = {"after_two_wrapper_closes": after_wrapper, "after_owner_closes_twice": after_owner}
c.check("B. borrowed: two AuditedMemoryStore closes leave the engine undisposed with a live server connection, store usable",
        after_wrapper["disposals"] == 0 and after_wrapper["connections"] >= 1 and after_wrapper["store_get"]
        and after_wrapper["store_put_and_get_after_wrapper_close"], after_wrapper)
c.check("B. borrowed: the owner's two closes dispose the engine once and leave no connection",
        after_owner == {"disposals": 1, "connections": 0}, after_owner)


# ---------------------------------------------------------------- C. scheduler still running
class Scheduler:
    def __init__(self) -> None:
        self.live = True

    def bind_record_source(self, record_source) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def is_alive(self) -> bool:
        return self.live


db = Database("scheduler")
stores.clear()
sched_disposals: list[object] = []


def record_backend_c(**kwargs):
    resolved = resolve_backend(**kwargs)
    stores.append(resolved.store)
    event.listen(resolved.store._engine, "engine_disposed", sched_disposals.append)
    return resolved


runtime_memory.resolve_memory_backend = record_backend_c
artifact_closes: list[int] = []


class FakeArtifactStore:
    def close(self) -> None:
        artifact_closes.append(1)


try:
    config = OpenMinionConfig()
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"
    mem_root = WORK / "scheduler-root"
    scheduler = Scheduler()
    base_fn = pg_memory_config(db.url)
    asm = runtime_memory.build_memory_v2_runtime_assembly(
        config=config, agent_id="alpha", memory_root=mem_root, logger=logging.getLogger("probe_368_pg"),
        config_manager=None, home_root=mem_root, data_root=mem_root, session_context=None, retrieve_ctl=None,
        storage_path=None, scheduler=scheduler, artifactctl_factory=FakeArtifactStore,
        resolve_runtime_memory_config_fn=lambda **kw: base_fn(base_config=config, home_root=mem_root, data_root=mem_root),
    )
    asm.service.write_record(scope="agent:alpha", record_type="fact", title="Before", content="x")
    blocked = [asm.close(), asm.close()]
    while_blocked = {"disposals": len(sched_disposals), "artifact_closes": len(artifact_closes),
                     "connections": db.connections()}
    rec = asm.service.write_record(scope="agent:alpha", record_type="fact", title="While blocked", content="y")
    rid = getattr(rec, "id", None) or (rec.get("id") if isinstance(rec, dict) else rec)
    while_blocked["store_usable"] = asm.delegated_store.get_record(str(rid)) is not None
    scheduler.live = False
    done = [asm.close(), asm.close()]
    after_stop = {"disposals": len(sched_disposals), "artifact_closes": len(artifact_closes), "connections": db.settled()}
finally:
    runtime_memory.resolve_memory_backend = resolve_backend
    for s in stores:
        s._engine.dispose()
    db.drop()
observed["C_scheduler_postgres"] = {"store": [type(s).__name__ for s in stores], "blocked": [repr(r) for r in blocked],
                                    "while_blocked": while_blocked, "after_stop": [repr(r) for r in done],
                                    "after_stop_counts": after_stop, "same_result_object": done[0] is done[1]}
c.check("C. live scheduler: close() x2 is scheduler_still_running; engine not disposed, artifact store not closed, store usable",
        [type(s).__name__ for s in stores] == ["PostgresMemoryStore"]
        and all(r.reason_code == "scheduler_still_running" and not r.closed for r in blocked)
        and while_blocked["disposals"] == 0 and while_blocked["artifact_closes"] == 0
        and while_blocked["connections"] >= 1 and while_blocked["store_usable"], while_blocked)
c.check("C. after the scheduler stops, close succeeds once: one disposal, one artifact close, no connections",
        done[0].closed and done[0] is done[1] and after_stop == {"disposals": 1, "artifact_closes": 1, "connections": 0},
        after_stop)

admin.dispose()
write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items, "observed": observed})
print(f"{c.summary()['passed']}/{c.summary()['total']} checks pass")
sys.exit(0 if c.all_pass else 1)
