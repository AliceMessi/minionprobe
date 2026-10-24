"""#368 (SQLite, mock, remote): the runtime memory assembly closes the store it creates.

  A. SDK (default SQLite memory): default echo runtime, explicit roots; the store
     returned by resolve_memory_backend and the memory assembly's artifact store
     (a real ArtifactCtl the assembly obtains and injects into the store) are
     counted; one turn; runtime.close() three times.
  B. Builder, per backend (sqlite, mock, remote): counting subclasses of the three
     store classes, an injected artifact store from artifactctl_factory; an audited
     write and a delegated read (sqlite, mock); close() twice. Expected order:
     store.close, artifactctl.close, once each; the delegated backend reads the
     same AuditedMemoryStore and has no closer of its own.
  C. Owned artifact resource: no injected artifact store (factory raises), so the
     SQLite store creates its own ArtifactCtl (a real one on explicit roots) when it
     records an evidence reference; close() twice closes it once.
  D. Injected/borrowed controls: a store built with an injected artifact store does
     not close it; AuditedMemoryStore(store) without owns_store does not close the
     store; the owner still can.
  E. scheduler_still_running (sqlite, mock): store and artifact store stay open and
     usable; after the scheduler stops, close succeeds exactly once.

B-E call the installed builder `openminion.services.runtime.memory` and
`openminion.modules.memory.storage.factory` directly: APIRuntime exposes no way to
inject an artifact factory or a scheduler.

Usage: python -I probe_368.py <work_dir> <out.json>
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, echo_runtime, outcome, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()
info = banner("probe_368")

from openminion import Agent  # noqa: E402
from openminion.base.config import OpenMinionConfig  # noqa: E402
from openminion.modules.artifact.config import from_base_config as artifact_config_for  # noqa: E402
from openminion.modules.artifact.control import ArtifactCtl  # noqa: E402
from openminion.modules.memory.storage import factory as store_factory  # noqa: E402
from openminion.modules.memory.storage.audit import AuditedMemoryStore  # noqa: E402
from openminion.modules.memory.storage.sqlite import store as sqlite_store_module  # noqa: E402
from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore  # noqa: E402
from openminion.services.runtime import memory as runtime_memory  # noqa: E402

c = Checks()
observed: dict[str, object] = {}


def closed_db(action) -> str:
    try:
        action()
        return "accepted"
    except sqlite3.ProgrammingError as exc:
        return f"ProgrammingError: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- A. SDK, SQLite
stores: list[object] = []
artifact_stores: list[object] = []
store_closed: list[int] = []
artifact_closed: list[int] = []
resolve_backend = runtime_memory.resolve_memory_backend
runtime_artifactctl = runtime_memory._runtime_artifactctl
orig_store_close, orig_artifact_close = SQLiteMemoryStore.close, ArtifactCtl.close


def record_backend(**kwargs):
    resolved = resolve_backend(**kwargs)
    stores.append(resolved.store)
    return resolved


def record_artifactctl(*args, **kwargs):
    ctl = runtime_artifactctl(*args, **kwargs)
    artifact_stores.append(ctl)
    return ctl


def counting_store_close(self):
    store_closed.append(id(self))
    orig_store_close(self)


def counting_artifact_close(self):
    artifact_closed.append(id(self))
    orig_artifact_close(self)


runtime_memory.resolve_memory_backend = record_backend
runtime_memory._runtime_artifactctl = record_artifactctl
SQLiteMemoryStore.close = counting_store_close  # type: ignore[method-assign]
ArtifactCtl.close = counting_artifact_close  # type: ignore[method-assign]
root = WORK / "sdk-root"
runtime = echo_runtime(root)
try:
    with Agent(runtime=runtime) as agent:
        run_state = agent.run("hello").run_state
    [store] = stores or [None]
    [ctl] = artifact_stores or [None]
    injected = {
        "store_type": type(store).__name__,
        "store_artifactctl_is_assembly_artifactctl": getattr(store, "_artifactctl", None) is ctl,
        "store_owns_artifactctl": getattr(store, "_owns_artifactctl", None),
    }
    counts = []
    for _ in range(3):
        runtime.close()
        counts.append({"store_close_calls": store_closed.count(id(store)),
                       "assembly_artifactctl_close_calls": artifact_closed.count(id(ctl))})
    ctl_after = closed_db(lambda: ctl.ingest_bytes(b"after shutdown"))
finally:
    runtime.close()
    runtime_memory.resolve_memory_backend = resolve_backend
    runtime_memory._runtime_artifactctl = runtime_artifactctl
    SQLiteMemoryStore.close = orig_store_close  # type: ignore[method-assign]
    ArtifactCtl.close = orig_artifact_close  # type: ignore[method-assign]
observed["A_sdk_sqlite"] = {"run_state": run_state, "stores_created": len(stores), **injected,
                            "after_each_runtime_close": counts, "assembly_artifactctl_write_after_shutdown": ctl_after}
c.check("A. echo SDK turn completes with one SQLiteMemoryStore from resolve_memory_backend",
        run_state == "completed" and len(stores) == 1 and injected["store_type"] == "SQLiteMemoryStore",
        {"run_state": run_state, "stores": [type(s).__name__ for s in stores]})
c.check("A. the assembly's ArtifactCtl is injected into the store, which does not own it",
        injected["store_artifactctl_is_assembly_artifactctl"] and injected["store_owns_artifactctl"] is False, injected)
c.check("A. runtime.close() x3: store closed once, assembly ArtifactCtl closed once",
        counts == [{"store_close_calls": 1, "assembly_artifactctl_close_calls": 1}] * 3, counts)
c.check("A. the assembly ArtifactCtl rejects writes after shutdown (closed database)",
        ctl_after.startswith("ProgrammingError"), ctl_after)


# ---------------------------------------------------------------- shared builder helpers
class FakeArtifactStore:
    """Injected artifact store: records close calls in the shared event list."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("artifactctl.close")


class Scheduler:
    def __init__(self, *, live_after_stop: bool) -> None:
        self.live = live_after_stop

    def bind_record_source(self, record_source) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def is_alive(self) -> bool:
        return self.live


def assembly(memory_root: Path, memory_config=None, **overrides):
    config = OpenMinionConfig()
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"
    args = dict(
        config=config, agent_id="alpha", memory_root=memory_root,
        logger=logging.getLogger("probe_368"), config_manager=None, home_root=None,
        data_root=None, session_context=None, retrieve_ctl=None, storage_path=None,
    )
    if memory_config is not None:
        args["resolve_runtime_memory_config_fn"] = lambda **kwargs: memory_config
    args.update(overrides)
    return runtime_memory.build_memory_v2_runtime_assembly(**args)


EVENTS: list[str] = []
originals = {name: getattr(store_factory, name) for name in ("SQLiteMemoryStore", "InMemoryMemoryStore", "RemoteMemoryStore")}


def counting(store_type):
    class CountingStore(store_type):
        def close(self) -> None:
            EVENTS.append("store.close")
            super().close()

    CountingStore.__name__ = f"Counting{store_type.__name__}"
    return CountingStore


BACKENDS = {
    "sqlite": None,
    "mock": {"store": {"backend": "mock"}},
    "remote": {"store": {"backend": "remote", "remote": {"endpoint": "http://127.0.0.1:9"}}},
}

for name in originals:
    setattr(store_factory, name, counting(originals[name]))
try:
    # ------------------------------------------------------------ B. per backend
    rows = {}
    for backend, memory_config in BACKENDS.items():
        EVENTS.clear()
        asm = assembly(WORK / f"builder-{backend}", memory_config,
                       artifactctl_factory=lambda: FakeArtifactStore(EVENTS))
        created_store = asm.service._store._store
        delegated = asm.delegated_store
        read = "not attempted (remote endpoint is not served)"
        if backend != "remote":
            rec = asm.service.write_record(scope="agent:alpha", record_type="fact", title="T", content="C")
            rid = getattr(rec, "id", None) or (rec.get("id") if isinstance(rec, dict) else rec)
            got = delegated.get_record(str(rid))
            read = "found" if got is not None else "missing"
        first, second = asm.close(), asm.close()
        rows[backend] = {
            "created_store": type(created_store).__name__,
            "service_closes": type(asm.service._store).__name__,
            "delegated_backend": type(delegated).__name__,
            "delegated_reads_same_audited_store": delegated.store is asm.service._store,
            "delegated_backend_has_close": hasattr(delegated, "close"),
            "delegated_read": read,
            "close_results": [repr(first), repr(second)],
            "same_result_object": first is second,
            "events": list(EVENTS),
        }
        c.check(f"B. {backend}: assembly wraps the created store in AuditedMemoryStore; close() x2 returns one closed result",
                rows[backend]["service_closes"] == "AuditedMemoryStore" and first is second and first.closed, rows[backend])
        c.check(f"B. {backend}: delegated reads use the same audited store and add no closer",
                rows[backend]["delegated_reads_same_audited_store"] and not rows[backend]["delegated_backend_has_close"]
                and read in ("found", "not attempted (remote endpoint is not served)"),
                {k: rows[backend][k] for k in ("delegated_backend", "delegated_reads_same_audited_store",
                                                "delegated_backend_has_close", "delegated_read")})
        c.check(f"B. {backend}: the created store closes once, then the injected artifact store once",
                EVENTS == ["store.close", "artifactctl.close"], list(EVENTS))
    observed["B_builder_backends"] = rows

    # ------------------------------------------------------------ C. owned artifact resource
    EVENTS.clear()
    art_root = WORK / "owned-artifacts"
    real_ctl = ArtifactCtl(artifact_config_for(base_config=OpenMinionConfig(), home_root=art_root,
                                               data_root=art_root / ".openminion"))
    sha = real_ctl.ingest_bytes(b"evidence for probe 368").sha256
    ctl_closes: list[int] = []
    ref_adds: list[str] = []
    real_close, real_ref_add = real_ctl.close, real_ctl.ref_add

    def ctl_close():
        ctl_closes.append(1)
        real_close()

    def ctl_ref_add(owner_type, owner_id, target):
        ref_adds.append(owner_type)
        return real_ref_add(owner_type, owner_id, target)

    real_ctl.close = ctl_close
    real_ctl.ref_add = ctl_ref_add
    factory_calls: list[int] = []
    orig_default = sqlite_store_module.create_default_artifactctl

    def store_default_artifactctl(config=None):
        factory_calls.append(1)
        return real_ctl

    def unavailable():
        raise OSError("artifact root unavailable")

    sqlite_store_module.create_default_artifactctl = store_default_artifactctl
    try:
        asm = assembly(WORK / "builder-owned-artifact", artifactctl_factory=unavailable)
        created_store = asm.service._store._store
        owns_before = created_store._owns_artifactctl
        asm.service.write_record(scope="agent:alpha", record_type="fact", title="Referenced",
                                 content="Referenced content", evidence_refs=[sha])
        first, second = asm.close(), asm.close()
        after = closed_db(lambda: real_ctl.ingest_bytes(b"after"))
    finally:
        sqlite_store_module.create_default_artifactctl = orig_default
    observed["C_owned_artifact"] = {
        "assembly_artifactctl": repr(asm.artifactctl), "store_owns_its_artifactctl": owns_before,
        "store_created_artifactctl_calls": len(factory_calls), "ref_add_calls": len(ref_adds),
        "close_results": [repr(first), repr(second)], "artifactctl_close_calls": len(ctl_closes),
        "store_close_events": EVENTS.count("store.close"), "write_after_close": after,
    }
    c.check("C. with no injected artifact store the SQLite store creates and owns one real ArtifactCtl",
            asm.artifactctl is None and owns_before is True and len(factory_calls) == 1 and len(ref_adds) == 1,
            observed["C_owned_artifact"])
    c.check("C. close() x2: the store closes once and closes its owned ArtifactCtl once",
            first is second and EVENTS.count("store.close") == 1 and len(ctl_closes) == 1
            and after.startswith("ProgrammingError"),
            {"store_closes": EVENTS.count("store.close"), "artifactctl_closes": len(ctl_closes), "write_after_close": after})

    # ------------------------------------------------------------ D. injected / borrowed controls
    EVENTS.clear()
    resolved = store_factory.resolve_memory_backend(
        config=runtime_memory.memory_runtime_configuration.resolve_runtime_memory_config(
            config=OpenMinionConfig(), memory_root=WORK / "injected"),
        db_path=WORK / "injected" / "memory.db", artifactctl=FakeArtifactStore(EVENTS))
    resolved.store.close()
    resolved.store.close()
    injected_events = list(EVENTS)
    EVENTS.clear()
    borrowed = AuditedMemoryStore(resolved.store)
    borrowed.close()
    borrowed.close()
    borrowed_events = list(EVENTS)
    from openminion.modules.memory.models import MemoryRecord

    def put_and_get() -> None:
        rid = resolved.store.put(MemoryRecord(
            id="borrowed-r1", scope="session:s1", type="fact", title="borrowed", content={"text": "borrowed"},
            created_at="2026-10-24T00:00:00+00:00", updated_at="2026-10-24T00:00:00+00:00"))
        assert resolved.store.get(rid) is not None

    still_usable = outcome(put_and_get)
    observed["D_controls"] = {"store_with_injected_artifactctl_close_x2_events": injected_events,
                              "borrowed_audited_wrapper_close_x2_events": borrowed_events,
                              "store_usable_after_borrowed_wrapper_closed": still_usable}
    c.check("D. a store built with an injected artifact store closes twice without closing it",
            injected_events == ["store.close", "store.close"], injected_events)
    c.check("D. AuditedMemoryStore(store) without owns_store never closes the borrowed store",
            borrowed_events == [] and still_usable == "ok", {"events": borrowed_events, "usable": still_usable})

    # ------------------------------------------------------------ E. scheduler still running
    sched_rows = {}
    for backend in ("sqlite", "mock"):
        EVENTS.clear()
        scheduler = Scheduler(live_after_stop=True)
        asm = assembly(WORK / f"scheduler-{backend}", BACKENDS[backend], scheduler=scheduler,
                       artifactctl_factory=lambda: FakeArtifactStore(EVENTS))
        blocked = [asm.close(), asm.close()]
        events_blocked = list(EVENTS)
        rec = asm.service.write_record(scope="agent:alpha", record_type="fact", title="While blocked", content="x")
        rid = getattr(rec, "id", None) or (rec.get("id") if isinstance(rec, dict) else rec)
        usable = asm.delegated_store.get_record(str(rid)) is not None
        scheduler.live = False
        done = [asm.close(), asm.close()]
        sched_rows[backend] = {"blocked": [repr(r) for r in blocked], "events_while_blocked": events_blocked,
                               "store_usable_while_blocked": usable, "after_stop": [repr(r) for r in done],
                               "same_result_object": done[0] is done[1], "events": list(EVENTS)}
        c.check(f"E. {backend}: live scheduler: close() x2 is scheduler_still_running; store and artifact store not closed, store usable",
                all(r.reason_code == "scheduler_still_running" and not r.closed for r in blocked)
                and events_blocked == [] and usable, sched_rows[backend])
        c.check(f"E. {backend}: after the scheduler stops, close succeeds once (store.close, artifactctl.close) and repeats",
                done[0].closed and done[0] is done[1] and EVENTS == ["store.close", "artifactctl.close"], list(EVENTS))
    observed["E_scheduler"] = sched_rows
finally:
    for name, original in originals.items():
        setattr(store_factory, name, original)

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items, "observed": observed})
print(f"{c.summary()['passed']}/{c.summary()['total']} checks pass")
sys.exit(0 if c.all_pass else 1)
