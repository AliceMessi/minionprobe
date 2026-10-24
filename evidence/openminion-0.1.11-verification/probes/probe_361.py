"""#361: the runtime-created memory audit sink closes exactly once.

  A. SDK (the issue's reproduction, without repo fixtures): default echo runtime,
     single process, explicit roots; every SQLiteMemoryAuditSink counted; one turn;
     runtime.close() three times.
  B. Builder: the memory assembly closes the sink it creates once (one audited
     write, close() twice).
  C. scheduler_still_running: a scheduler stub that stays alive after stop(); the
     sink stays open and keeps accepting audit events; after it stops, one close.
  D. Borrowed sinks: AuditedMemoryStore(store, sink=sink) with the default
     owns_sink=False leaves the sink open across two closes; an owned sink closes once.

B and C call the installed builder `openminion.services.runtime.memory` directly:
APIRuntime exposes no way to inject a scheduler. D uses the installed
AuditedMemoryStore with a real SQLiteMemoryAuditSink and InMemoryMemoryStore.

Usage: python -I probe_361.py <work_dir> <out.json>
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, echo_runtime, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()
info = banner("probe_361")

from openminion import Agent  # noqa: E402
from openminion.base.config import OpenMinionConfig  # noqa: E402
from openminion.modules.memory.models import MemoryRecord  # noqa: E402
from openminion.modules.memory.storage.audit import (  # noqa: E402
    AuditedMemoryStore,
    SQLiteMemoryAuditSink,
)
from openminion.modules.memory.storage.memory import InMemoryMemoryStore  # noqa: E402
from openminion.services.runtime import memory as runtime_memory  # noqa: E402

c = Checks()
observed: dict[str, object] = {}


def audit_rows(path: Path) -> int | str:
    """Count audit events through a separate read-only connection that is closed at once."""
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT count(*) FROM memory_audit_events").fetchone()[0]
    except sqlite3.Error as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def connection_closed(conn: sqlite3.Connection | None) -> bool | None:
    if conn is None:
        return None
    try:
        conn.execute("SELECT 1")
        return False
    except sqlite3.ProgrammingError:
        return True


# ---------------------------------------------------------------- A. SDK
created: list[SQLiteMemoryAuditSink] = []
closed_ids: list[int] = []
memory_sinks: list[SQLiteMemoryAuditSink] = []
original_init, original_close = SQLiteMemoryAuditSink.__init__, SQLiteMemoryAuditSink.close
create_memory_sink = runtime_memory.SQLiteMemoryAuditSink


def record_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    created.append(self)


def record_close(self):
    closed_ids.append(id(self))
    original_close(self)


def record_memory_sink(db_path):
    sink = create_memory_sink(db_path)
    memory_sinks.append(sink)
    return sink


SQLiteMemoryAuditSink.__init__ = record_init  # type: ignore[method-assign]
SQLiteMemoryAuditSink.close = record_close  # type: ignore[method-assign]
runtime_memory.SQLiteMemoryAuditSink = record_memory_sink  # type: ignore[assignment]
root = WORK / "sdk-root"
runtime = echo_runtime(root)
try:
    with Agent(runtime=runtime) as agent:
        run_state = agent.run("hello").run_state
    open_after_turn = [s._conn is not None for s in created]
    conns_after_turn = [s._conn for s in created]
    events_after_turn = [audit_rows(s.db_path) for s in created]
    close_counts = []
    for _ in range(3):
        runtime.close()
        close_counts.append([closed_ids.count(id(s)) for s in created])
    open_after_close = [s._conn is not None for s in created]
    conn_closed_after = [connection_closed(conn) for conn in conns_after_turn]
finally:
    runtime.close()
    SQLiteMemoryAuditSink.__init__ = original_init  # type: ignore[method-assign]
    SQLiteMemoryAuditSink.close = original_close  # type: ignore[method-assign]
    runtime_memory.SQLiteMemoryAuditSink = create_memory_sink  # type: ignore[assignment]
    for s in created:
        original_close(s)

sink_paths = [str(s.db_path.relative_to(root)).replace("\\", "/") for s in created]
observed["A_sdk"] = {
    "run_state": run_state,
    "sinks_created": len(created),
    "sinks_created_by_runtime_memory_assembly": len(memory_sinks),
    "sink_paths_relative_to_runtime_root": sink_paths,
    "sink_connection_open_after_turn": open_after_turn,
    "audit_events_after_turn": events_after_turn,
    "sink_close_calls_after_each_runtime_close": close_counts,
    "sink_connection_attribute_set_after_close": open_after_close,
    "turn_connection_object_closed_after_shutdown": conn_closed_after,
}
c.check("A. echo SDK turn completes", run_state == "completed", run_state)
c.check("A. exactly one audit sink is created, by the runtime memory assembly",
        len(created) == 1 and created == memory_sinks, {"created": len(created), "by_assembly": len(memory_sinks)})
c.check("A. the sink's database is <memory root>/memory.audit.db under the runtime root",
        len(sink_paths) == 1 and sink_paths[0].endswith("/memory.audit.db"), sink_paths)
c.check("A. the audited turn opens the runtime-created sink's connection", open_after_turn == [True], open_after_turn)
c.check("A. runtime.close() x3 closes the sink exactly once", close_counts == [[1], [1], [1]], close_counts)
c.check("A. after shutdown the sink holds no connection", open_after_close == [False], open_after_close)
c.check("A. the connection the turn opened is closed (execute raises ProgrammingError)",
        conn_closed_after == [True], conn_closed_after)


# ---------------------------------------------------------------- B/C builder
class CountingAuditSink(SQLiteMemoryAuditSink):
    instances: list["CountingAuditSink"] = []

    def __init__(self, db_path) -> None:
        super().__init__(db_path)
        self.close_calls = 0
        CountingAuditSink.instances.append(self)

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class Scheduler:
    def __init__(self, *, live_after_stop: bool) -> None:
        self.live = live_after_stop
        self.stops = 0

    def bind_record_source(self, record_source) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.stops += 1

    def is_alive(self) -> bool:
        return self.live


def assembly(memory_root: Path, **overrides):
    config = OpenMinionConfig()
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"
    args = dict(
        config=config, agent_id="alpha", memory_root=memory_root,
        logger=logging.getLogger("probe_361"), config_manager=None, home_root=None,
        data_root=None, session_context=None, retrieve_ctl=None, storage_path=None,
        artifactctl_factory=lambda: None,
    )
    args.update(overrides)
    return runtime_memory.build_memory_v2_runtime_assembly(**args)


def write(asm, title: str) -> None:
    asm.service.write_record(scope="agent:alpha", record_type="fact", title=title, content=f"{title} content")


runtime_memory.SQLiteMemoryAuditSink = CountingAuditSink  # type: ignore[assignment]
try:
    # B. owned sink, closed once by the assembly
    CountingAuditSink.instances.clear()
    asm = assembly(WORK / "builder-owned")
    write(asm, "Audited")
    [sink] = CountingAuditSink.instances
    opened = sink._conn is not None
    first, second = asm.close(), asm.close()
    observed["B_builder"] = {
        "sink": str(sink.db_path.name), "open_after_write": opened,
        "first": repr(first), "second_is_first": first is second,
        "close_calls": sink.close_calls, "conn_after_close": sink._conn is not None,
    }
    c.check("B. builder: an audited write opens the sink it creates", opened)
    c.check("B. builder: close() twice returns the same closed result", first is second and first.closed, repr(first))
    c.check("B. builder: the created sink is closed exactly once", sink.close_calls == 1 and sink._conn is None,
            {"close_calls": sink.close_calls, "conn_set": sink._conn is not None})

    # C. scheduler still running
    CountingAuditSink.instances.clear()
    scheduler = Scheduler(live_after_stop=True)
    asm = assembly(WORK / "builder-scheduler", scheduler=scheduler)
    write(asm, "Before")
    [sink] = CountingAuditSink.instances
    service_closes: list[int] = []
    service_close = asm.service.close

    def counting_service_close():
        service_closes.append(1)
        return service_close()

    asm.service.close = counting_service_close
    blocked = [asm.close(), asm.close()]
    still_open = sink._conn is not None
    sink_closes_blocked = sink.close_calls
    service_closes_blocked = len(service_closes)
    write(asm, "While blocked")
    events_while_blocked = len(sink.list_events())
    scheduler.live = False
    done = [asm.close(), asm.close()]
    observed["C_scheduler_still_running"] = {
        "blocked": [repr(r) for r in blocked], "sink_close_calls_while_blocked": sink_closes_blocked,
        "service_close_calls_while_blocked": service_closes_blocked, "sink_open_while_blocked": still_open,
        "audit_events_while_blocked": events_while_blocked,
        "after_stop": [repr(r) for r in done], "sink_close_calls": sink.close_calls,
        "service_close_calls": len(service_closes), "scheduler_stop_calls": scheduler.stops,
    }
    c.check("C. scheduler alive after stop(): close() x2 returns scheduler_still_running",
            all((not r.closed) and r.reason_code == "scheduler_still_running" for r in blocked), [repr(r) for r in blocked])
    c.check("C. while blocked: 0 sink closes, 0 service closes, the sink stays open and accepts audit events (2 events)",
            still_open and sink_closes_blocked == 0 and service_closes_blocked == 0 and events_while_blocked == 2,
            {"open": still_open, "sink_closes": sink_closes_blocked, "service_closes": service_closes_blocked,
             "events": events_while_blocked})
    c.check("C. after the scheduler stops, close() succeeds and repeats the same result",
            done[0].closed and done[1] is done[0], [repr(r) for r in done])
    c.check("C. the sink and the memory service are each closed exactly once",
            sink.close_calls == 1 and sink._conn is None and len(service_closes) == 1,
            {"sink_close_calls": sink.close_calls, "service_close_calls": len(service_closes)})
finally:
    runtime_memory.SQLiteMemoryAuditSink = create_memory_sink  # type: ignore[assignment]
    for s in CountingAuditSink.instances:
        SQLiteMemoryAuditSink.close(s)


# ---------------------------------------------------------------- D. borrowed
def record(rid: str) -> MemoryRecord:
    return MemoryRecord(id=rid, scope="session:s1", type="fact", title=rid, content={"text": rid},
                        created_at="2026-10-24T00:00:00+00:00", updated_at="2026-10-24T00:00:00+00:00")


default_owns_sink = inspect.signature(AuditedMemoryStore).parameters["owns_sink"].default
c.check("D. AuditedMemoryStore keeps owns_sink=False as its default", default_owns_sink is False, default_owns_sink)

borrowed_sink = CountingAuditSink(WORK / "borrowed" / "memory.audit.db")
borrowed_store = AuditedMemoryStore(InMemoryMemoryStore(), sink=borrowed_sink)
borrowed_store.put(record("r1"))
conn_before = borrowed_sink._conn
borrowed_store.close()
borrowed_store.close()
after_close = {
    "close_calls": borrowed_sink.close_calls,
    "same_connection": borrowed_sink._conn is conn_before and conn_before is not None,
    "connection_usable": connection_closed(conn_before) is False,
    "events_readable": len(borrowed_sink.list_events()),
}
borrowed_sink.close()
after_owner_close = {"close_calls": borrowed_sink.close_calls, "conn_set": borrowed_sink._conn is not None}
observed["D_borrowed_sink"] = {"after_two_store_closes": after_close, "after_owner_closes_it": after_owner_close}
c.check("D. borrowed sink: two store closes leave it open (0 close calls, same live connection, events readable)",
        after_close["close_calls"] == 0 and after_close["same_connection"] and after_close["connection_usable"]
        and after_close["events_readable"] == 1, after_close)
c.check("D. borrowed sink: its owner can still close it", after_owner_close == {"close_calls": 1, "conn_set": False},
        after_owner_close)

owned_sink = CountingAuditSink(WORK / "owned" / "memory.audit.db")
owned_store = AuditedMemoryStore(InMemoryMemoryStore(), sink=owned_sink, owns_sink=True)
owned_store.put(record("r1"))
owned_store.close()
owned_store.close()
observed["D_owned_sink_control"] = {"close_calls": owned_sink.close_calls, "conn_set": owned_sink._conn is not None}
c.check("D. owned control: AuditedMemoryStore(owns_sink=True) closes its sink once across two closes",
        owned_sink.close_calls == 1 and owned_sink._conn is None, observed["D_owned_sink_control"])

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items, "observed": observed})
print(f"{c.summary()['passed']}/{c.summary()['total']} checks pass")
sys.exit(0 if c.all_pass else 1)
