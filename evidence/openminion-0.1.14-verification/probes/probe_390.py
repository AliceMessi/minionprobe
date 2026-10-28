"""#390 on the installed package: a runtime memory assembly build that fails
partway closes what it obtained, once, and keeps its own error.

    python -I -X utf8 probe_390.py <work_dir> <out.json> [--postgres]

In-process (the checklist needs GC control and close counters): cyclic GC is
disabled before openminion is imported and gc.collect is replaced by a counter
that does not collect (0 calls expected), so only explicit close() and
reference counting can release anything. The probe calls the installed
`openminion.services.runtime.memory.build_memory_v2_runtime_assembly` with its
public parameters; the artifact store is the package default
(`create_default_artifactctl`, its SQLite index under OPENMINION_DATA_ROOT,
set to the case folder). Close accounting wraps __init__ and close() of the
package classes the build obtains (ArtifactCtl, SQLiteMemoryStore,
InMemoryMemoryStore, RemoteMemoryStore, PostgresMemoryStore,
SQLiteMemoryAuditSink, AuditedMemoryStore, MemoryService,
BackendMemoryStoreAdapter); the wrappers call through and hold instances by
weak reference only.

Failure points (S = SQLite and the other built-in stores, every platform):
- after the artifact store: a remote store without an endpoint (real error);
  an external provider whose adapter is not registered (real error);
- after the memory store: the memory root is a file, so the audit sink cannot
  be created (real FileExistsError), and, as upstream's test does, the
  builder's SQLiteMemoryAuditSink replaced by a raising callable;
- after the audited store (store + audit sink): the builder's next call,
  memory_runtime_configuration.merged_ranking_config, replaced by a raising
  callable;
- after the memory service: runtime.memory_retrieval_max_chars that is not a
  number (real ValueError while the gateway is built), and adapter_cls (a
  public parameter) raising, for the sqlite, mock and remote stores and for an
  external backend the service owns;
- the same with the owner's and/or the artifact store's close() raising after
  it closed (the build's own exception object must reach the caller);
- with a caller-owned VectorSyncScheduler and VectorIndexAdapter (package
  classes over a SQLiteVecBackend; a deterministic embedding provider): they
  must not be stopped or closed, and must still work, also in a later
  successful assembly.
Right after each failure, still in the except block (the exception and its
traceback alive) with GC off: the sqlite3 connections open on the case's
files (gc.get_objects, which does not collect), then every SQLite file is
renamed away and back and deleted and the case folders are removed. After
the except block: open connections and leftover files again.

Controls: a successful SQLite build (nothing closed during the build; the
assembly holds and uses its resources; assembly.close() closes each once, a
second close changes nothing; then the files go at once) and a successful
external-backend build (no built-in store or audit sink; the backend closes
once with the assembly).

PostgreSQL (--postgres, OPENMINION_TEST_POSTGRES_URL): failures after the
store, the audited store and the service, a failing artifact-store close, and
a successful control; each case in its own schema with its own
application_name. The owned engine's engine_disposed events are counted and
pg_stat_activity is read at the failure (from the injected step) and after.
"""

from __future__ import annotations

import gc

gc.disable()
GC_CALLS = {"n": 0}


def _counting_collect(*_args, **_kwargs):
    GC_CALLS["n"] += 1
    return 0


gc.collect = _counting_collect

import hashlib  # noqa: E402
import importlib.metadata as metadata  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Callable  # noqa: E402
from urllib.parse import quote_plus  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_390_lib as lib  # noqa: E402

lib.install_sqlite_tracking()
from _common import Checks, banner, clear_override_env, quiet, sanitize, write_result  # noqa: E402

import openminion  # noqa: E402
from openminion.base.config import OpenMinionConfig  # noqa: E402
from openminion.modules.artifact.control import ArtifactCtl  # noqa: E402
from openminion.modules.memory.backends import BackendMemoryStoreAdapter  # noqa: E402
from openminion.modules.memory.backends.external import register_external_backend  # noqa: E402
from openminion.modules.memory.backends.external.registry import ExternalBackendCapabilities  # noqa: E402
from openminion.modules.memory.service import MemoryService  # noqa: E402
from openminion.modules.memory.storage import AuditedMemoryStore, SQLiteMemoryAuditSink  # noqa: E402
from openminion.modules.memory.storage import factory as store_factory  # noqa: E402
from openminion.modules.storage.runtime.vector_index import SQLiteVecBackend  # noqa: E402
from openminion.modules.storage.runtime.vector_sync import VectorSyncScheduler  # noqa: E402
from openminion.services.runtime import memory as rm  # noqa: E402
from sophiagraph.storage import SophiaGraphSqliteStore  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
POSTGRES = "--postgres" in sys.argv[3:]
PG_BASE = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()
ROOT = WORK / "run"
LOG = logging.getLogger("probe390")
c = Checks()
results: dict[str, Any] = {}

tracker = lib.Tracker()
tracker.track(ArtifactCtl, "ArtifactCtl")
for _name in ("SQLiteMemoryStore", "InMemoryMemoryStore", "RemoteMemoryStore", "PostgresMemoryStore"):
    tracker.track(getattr(store_factory, _name), _name)
tracker.track(SQLiteMemoryAuditSink, "SQLiteMemoryAuditSink")
tracker.track(AuditedMemoryStore, "AuditedMemoryStore")
tracker.track(MemoryService, lambda obj, a, k: "MemoryService (owner)" if k.get("owns_store") else "MemoryService (non-owning)")
tracker.track(BackendMemoryStoreAdapter, "BackendMemoryStoreAdapter")
HashEmbedding, CallerOwnedVectorAdapter = lib.caller_vector_classes()
tracker.track(CallerOwnedVectorAdapter, "caller's vector adapter (close)")
tracker.track(VectorSyncScheduler, "caller's scheduler (stop)", method="stop")


class ProbeClosingReference(SophiaGraphSqliteStore):
    """The package's reference external backend (sophiagraph SQLite) with a close()."""

    def close(self) -> None:
        return None


tracker.track(ProbeClosingReference, "external backend")
register_external_backend(
    "probe-closing-reference",
    factory=lambda *, config, **_: ProbeClosingReference(Path(config.options["db_path"])),
    capabilities=ExternalBackendCapabilities(supports_relations=True, supports_candidate_workflow=True,
                                             supports_tier_history=True, supports_portability=True),
)


class GatewayUnavailable(RuntimeError):
    """The build's own failure after the memory service exists."""


def raising_gateway(holder: dict, hook: Callable[[], None] | None = None):
    def adapter_cls(*_args, **_kwargs):
        if hook is not None:
            hook()
        holder["exc"] = GatewayUnavailable("probe: gateway unavailable")
        raise holder["exc"]

    return adapter_cls


def runtime_config(**runtime: Any) -> OpenMinionConfig:
    config = OpenMinionConfig()
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"
    for key, value in runtime.items():
        setattr(config.runtime, key, value)
    return config


def build(case: Path, *, memory_config: Any = None, memory_root: Path | None = None,
          config: OpenMinionConfig | None = None, **overrides: Any):
    os.environ["OPENMINION_HOME"] = str(case / "home")
    os.environ["OPENMINION_DATA_ROOT"] = str(case / "data")
    args: dict[str, Any] = dict(config=config or runtime_config(), agent_id="alpha",
                                memory_root=memory_root or case / "memory", logger=LOG, config_manager=None,
                                home_root=None, data_root=None, session_context=None, retrieve_ctl=None,
                                storage_path=None)
    if memory_config is not None:
        args["resolve_runtime_memory_config_fn"] = lambda **_kwargs: memory_config
    return rm.build_memory_v2_runtime_assembly(**args, **overrides)


def replace_attr(owner: Any, name: str, value: Any) -> Callable[[], None]:
    original = getattr(owner, name)
    setattr(owner, name, value)
    return lambda: setattr(owner, name, original)


def inject_audit_sink_failure(hook: Callable[[], None] | None = None) -> Callable[[], None]:
    def unavailable_audit_sink(*_args, **_kwargs):
        if hook is not None:
            hook()
        raise OSError("probe: audit database unavailable")

    return replace_attr(rm, "SQLiteMemoryAuditSink", unavailable_audit_sink)


def inject_ranking_failure(hook: Callable[[], None] | None = None) -> Callable[[], None]:
    def unavailable_ranking(**_kwargs):
        if hook is not None:
            hook()
        raise LookupError("probe: ranking configuration unavailable")

    return replace_attr(rm.memory_runtime_configuration, "merged_ranking_config", unavailable_ranking)


def describe(exc: BaseException) -> dict[str, Any]:
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        frames.append(tb.tb_frame.f_code.co_name)
        tb = tb.tb_next
    message = str(exc)
    if getattr(exc, "winerror", None) is not None:  # the OS text is localized; keep the code and the file name
        message = f"[WinError {exc.winerror}] {Path(exc.filename).name if exc.filename else ''}".strip()
    return {"type": type(exc).__name__, "message": message, "raised in": frames[-1] if frames else None,
            "__context__": None if exc.__context__ is None else type(exc.__context__).__name__,
            "__cause__": None if exc.__cause__ is None else type(exc.__cause__).__name__}


AC = {"ArtifactCtl": [1]}
STORE = {**AC, "SQLiteMemoryStore": [1]}
AUDITED = {**STORE, "SQLiteMemoryAuditSink": [1], "AuditedMemoryStore": [1]}
SERVICE = {**AUDITED, "MemoryService (owner)": [1], "MemoryService (non-owning)": [0]}


def with_store(store: str) -> dict[str, list[int]]:
    out = {k: v for k, v in SERVICE.items() if k != "SQLiteMemoryStore"}
    out[store] = [1]
    return out


def case_folders(case: Path) -> list[Path]:
    return [p for p in sorted(case.iterdir()) if p.is_dir() and p.name != "caller"]


def fail_case(key: str, what: str, *, error: tuple[str, str], expected: dict[str, list[int]],
              kwargs: Callable[[Path], dict], inject: Callable[[], Callable[[], None]] | None = None,
              raise_after: tuple[str, ...] = (), holder: dict | None = None,
              before: Callable[[Path], dict] | None = None, allowed_left: tuple[str, ...] = ()) -> dict:
    tag = f"[{key}: {what}]"
    case = ROOT / key
    case.mkdir(parents=True)
    row: dict[str, Any] = {"what": what}
    tracker.case = key
    extra = before(case) if before else {}
    tracker.raise_after = set(raise_after)
    restore = inject() if inject else None
    caught = None
    exclude = (case / "caller",)
    try:
        try:
            assembly = build(case, **kwargs(case), **extra)
            row["build"] = "returned an assembly (no error)"
            assembly.close()
        except Exception as exc:  # noqa: BLE001 - the case's failure
            caught = describe(exc)
            if holder is not None:
                row["the caught exception is the build's own object"] = exc is holder.get("exc")
            # Immediately, in the handler: the exception and its traceback are alive, GC is off.
            row["in the handler: open sqlite3 connections on the case's files"] = lib.open_connections(case, exclude=exclude)
            row["in the handler: files before the operations"] = lib.files_under(case)
            row["in the handler: file operations"] = lib.immediate_file_ops(case, case_folders(case))
    finally:
        if restore is not None:
            restore()
        tracker.case = None
        tracker.raise_after = set()
    row["error"] = caught
    row["close calls (per instance)"] = tracker.calls(key)
    row["after the handler: open sqlite3 connections"] = lib.open_connections(case, exclude=exclude)
    left = [f for f in lib.files_under(case) if not f.startswith("caller/") and f not in allowed_left]
    row["after the handler: files left"] = left

    ok_error = bool(caught) and caught["type"] == error[0] and error[1] in caught["message"]
    c.check(f"{tag} the build raises {error[0]} ({error[1]!r})", ok_error, caught)
    c.check(f"{tag} each resource the build owned at the failure is closed exactly once; nothing else is closed",
            row["close calls (per instance)"] == expected,
            {"observed": row["close calls (per instance)"], "expected": expected})
    in_open = row.get("in the handler: open sqlite3 connections on the case's files") or {"count": None}
    c.check(f"{tag} right after the failure (except block, GC off): no sqlite3 connection open on the case's files",
            in_open["count"] == 0 and in_open.get("uninspectable") == 0, in_open)
    ops = row.get("in the handler: file operations") or {}
    c.check(f"{tag} right after the failure: every SQLite file renames away and back and deletes, the folders go",
            bool(ops) and all(v == "ok" for v in ops.values()), ops)
    after_open = row["after the handler: open sqlite3 connections"]
    c.check(f"{tag} after the except block: no open connection, no file left",
            after_open["count"] == 0 and not left, {"open": after_open, "files left": left})
    if holder is not None:
        own = row.get("the caught exception is the build's own object") is True
        c.check(f"{tag} the caller gets the build's own exception object (same identity, type and message, "
                f"raised in the failing step, no __context__ or __cause__) although a close raised",
                own and caught["type"] == "GatewayUnavailable" and caught["raised in"] == "adapter_cls"
                and caught["__context__"] is None and caught["__cause__"] is None,
                {"same object": row.get("the caught exception is the build's own object"), **(caught or {})})
    return row


def remote(case: Path) -> dict:
    return {"memory_config": {"store": {"backend": "remote"}}}


def sqlite_cases() -> None:
    results["S01"] = fail_case(
        "S01", "after the artifact store: remote store without an endpoint", kwargs=remote,
        error=("InvalidArgumentError", "remote backend requires remote.endpoint"), expected=AC)

    def memroot_file(case: Path) -> dict:
        (case / "memroot").write_text("not a folder\n", encoding="utf-8")
        return {"memory_root": case / "memroot",
                "memory_config": {"store": {"backend": "sqlite", "sqlite_path": str(case / "store" / "memory.db")}}}

    results["S02"] = fail_case(
        "S02", "after the memory store: the memory root is a file, the audit sink cannot be created",
        kwargs=memroot_file, error=("FileExistsError", ""), expected=STORE, allowed_left=("memroot",))
    results["S03"] = fail_case(
        "S03", "after the memory store: SQLiteMemoryAuditSink raises (injected)", kwargs=lambda case: {},
        inject=inject_audit_sink_failure, error=("OSError", "audit database unavailable"), expected=STORE)
    results["S04"] = fail_case(
        "S04", "after the audited store: merged_ranking_config raises (injected)", kwargs=lambda case: {},
        inject=inject_ranking_failure, error=("LookupError", "ranking configuration unavailable"), expected=AUDITED)
    results["S05"] = fail_case(
        "S05", "after the service: memory_retrieval_max_chars is not a number (gateway)",
        kwargs=lambda case: {"config": runtime_config(memory_retrieval_max_chars="not-a-number")},
        error=("ValueError", "not-a-number"), expected=SERVICE)
    stores = {"sqlite": (None, "SQLiteMemoryStore"),
              "mock": ({"store": {"backend": "mock"}}, "InMemoryMemoryStore"),
              "remote": ({"store": {"backend": "remote", "remote": {"endpoint": "http://127.0.0.1:9"}}},
                         "RemoteMemoryStore")}
    for key, (name, (memory_config, store_label)) in zip(("S06", "S07", "S08"), stores.items()):
        holder: dict = {}
        results[key] = fail_case(
            key, f"after the service: adapter_cls raises ({name} store)",
            kwargs=lambda case, m=memory_config, h=holder: {"memory_config": m, "adapter_cls": raising_gateway(h)},
            error=("GatewayUnavailable", "gateway unavailable"), expected=with_store(store_label))
    for key, failing, what in (("S09", ("MemoryService (owner)",), "the memory owner's close() raises"),
                               ("S10", ("ArtifactCtl",), "the artifact store's close() raises"),
                               ("S11", ("MemoryService (owner)", "ArtifactCtl"), "both close() calls raise")):
        holder = {}
        results[key] = fail_case(
            key, f"after the service, adapter_cls raises and {what}",
            kwargs=lambda case, h=holder: {"adapter_cls": raising_gateway(h)}, raise_after=failing, holder=holder,
            error=("GatewayUnavailable", "gateway unavailable"), expected=SERVICE)
    borrowed_case()
    results["S13"] = fail_case(
        "S13", "after the artifact store: external adapter not registered",
        kwargs=lambda case: {"memory_config": {"backend": {"provider": "external",
                                                            "external_adapter": "probe-not-registered"}}},
        error=("InvalidArgumentError", "No external backend registered"), expected=AC)
    holder = {}
    results["S14"] = fail_case(
        "S14", "after the service: adapter_cls raises (external backend owned by the service)",
        kwargs=lambda case, h=holder: {"memory_config": external_config(case), "adapter_cls": raising_gateway(h)},
        error=("GatewayUnavailable", "gateway unavailable"),
        expected={**AC, "external backend": [1], "BackendMemoryStoreAdapter": [1], "MemoryService (owner)": [1]})


def external_config(case: Path) -> dict:
    return {"backend": {"provider": "external", "external_adapter": "probe-closing-reference",
                        "options": {"db_path": str(case / "external" / "external.sqlite3")}}}


def borrowed_case() -> None:
    key = "S12"
    objects: dict[str, Any] = {}

    def make_borrowed(case: Path) -> dict:
        backend = SQLiteVecBackend(str(case / "caller" / "vectors.db"), dimension=8)
        objects["backend"] = backend
        objects["vector_adapter"] = CallerOwnedVectorAdapter(HashEmbedding(), backend)
        objects["scheduler"] = VectorSyncScheduler(objects["vector_adapter"], interval_seconds=3600)
        return {"vector_adapter": objects["vector_adapter"], "scheduler": objects["scheduler"]}

    holder: dict = {}
    row = fail_case(
        key, "after the service: adapter_cls raises, with a caller-owned scheduler and vector adapter",
        kwargs=lambda case: {"adapter_cls": raising_gateway(holder)}, before=make_borrowed,
        error=("GatewayUnavailable", "gateway unavailable"),
        expected={**SERVICE, "caller's vector adapter (close)": [0], "caller's scheduler (stop)": [0]})
    tag = f"[{key}]"
    case = ROOT / key
    vec, sched, backend = objects["vector_adapter"], objects["scheduler"], objects["backend"]
    usable: dict[str, Any] = {}
    usable["vector backend connection"] = lib.attempt(lambda: backend.conn.execute("SELECT count(*) FROM vector_entries").fetchone())
    usable["index a vector through the adapter"] = lib.attempt(
        lambda: vec.index_record(type("R", (), {"id": "probe-direct"})(), "probe direct text"))
    hits = vec.search("probe direct text") if usable["index a vector through the adapter"] == "ok" else []
    usable["search finds it"] = bool(hits) and hits[0][0] == "probe-direct"
    usable["scheduler alive (never started)"] = sched.is_alive()
    c.check(f"{tag} after the failed build the caller's vector adapter and its SQLite connection still work "
            "(no stop or close was called on them)",
            usable["vector backend connection"] == "ok" and usable["index a vector through the adapter"] == "ok"
            and usable["search finds it"] is True, usable)

    # The same caller-owned objects in a later, successful assembly.
    tracker.case = "S12-reuse"
    reuse: dict[str, Any] = {}
    try:
        assembly = build(case, vector_adapter=vec, scheduler=sched)
        assembly.start()
        reuse["scheduler alive after assembly.start()"] = sched.is_alive()
        rid = assembly.service.write_record(scope="agent:alpha", record_type="fact", title="Borrowed",
                                            content="Borrowed content")
        reuse["sync_now processed"] = sched.sync_now()
        reuse["scheduler stats"] = {k: v for k, v in sched.get_stats().items() if k != "last_sync_at"}
        found = vec.search("Borrowed\nBorrowed content")
        reuse["search top hit is the new record"] = bool(found) and found[0][0] == rid
        result = assembly.close()
        reuse["assembly.close()"] = {"closed": result.closed, "reason_code": result.reason_code}
        reuse["scheduler alive after assembly.close()"] = sched.is_alive()
    except Exception as exc:  # noqa: BLE001
        reuse["error"] = lib.err(exc)
    finally:
        tracker.case = None
    reuse["close calls in the successful assembly"] = tracker.calls("S12-reuse")
    vec.close()
    reuse["caller's calls overall (stop, close)"] = {k: v for k, v in tracker.calls(key).items() if k.startswith("caller")}
    reuse["after the caller closes its adapter: open connections on caller/"] = lib.open_connections(case / "caller")
    reuse["delete vectors.db"] = lib.attempt(lambda: shutil.rmtree(case / "caller"))
    row["borrowed objects after the failure"] = usable
    row["borrowed objects in a later successful assembly"] = reuse
    results[key] = row
    c.check(f"{tag} the same scheduler and vector adapter then serve a successful assembly: it starts the "
            "scheduler, the new record is synced and found, assembly.close() stops the scheduler once; the caller "
            "closes its adapter and vectors.db goes at once",
            "error" not in reuse and reuse.get("scheduler alive after assembly.start()") is True
            and reuse["scheduler stats"]["records_processed"] >= 1 and reuse["search top hit is the new record"] is True
            and reuse["assembly.close()"]["closed"] is True and reuse["scheduler alive after assembly.close()"] is False
            and reuse["caller's calls overall (stop, close)"] == {"caller's vector adapter (close)": [1],
                                                                 "caller's scheduler (stop)": [1]}
            and reuse["after the caller closes its adapter: open connections on caller/"]["count"] == 0
            and reuse["delete vectors.db"] == "ok", reuse)


def success_cases() -> None:
    # C01: a successful SQLite build keeps everything until assembly.close().
    key, case = "C01", ROOT / "C01"
    case.mkdir(parents=True)
    tag = "[C01: successful SQLite assembly lifetime]"
    row: dict[str, Any] = {}
    tracker.case = key
    try:
        assembly = build(case)
        row["close calls after the build"] = tracker.calls(key)
        row["assembly holds the artifact store and the owning service"] = (
            assembly.artifactctl is tracker.instance(key, "ArtifactCtl")
            and assembly.service is tracker.instance(key, "MemoryService (owner)"))
        rid = assembly.service.write_record(scope="agent:alpha", record_type="fact", title="Kept", content="Kept content")
        sink = tracker.instance(key, "SQLiteMemoryAuditSink")
        row["record written"] = bool(rid)
        row["audit events"] = [e["event_type"] for e in sink.list_events()] if sink else None
        row["open connections while the assembly lives"] = lib.open_connections(case)
        first = assembly.close()
        second = assembly.close()
        row["close results"] = {"first": [first.closed, first.reason_code], "second is first": second is first}
    except Exception as exc:  # noqa: BLE001
        row["error"] = lib.err(exc)
    finally:
        tracker.case = None
    row["close calls after two assembly.close()"] = tracker.calls(key)
    row["open connections after close"] = lib.open_connections(case)
    row["file operations after close"] = lib.immediate_file_ops(case, case_folders(case))
    results[key] = row
    zero = {k: [0] * len(v) for k, v in SERVICE.items()}
    alive = row.get("open connections while the assembly lives") or {"connections": []}
    c.check(f"{tag} the build closes nothing; the assembly holds its artifact store and service, which work "
            "(record written, audit event), with their SQLite handles open while it lives",
            "error" not in row and row["close calls after the build"] == zero
            and row["assembly holds the artifact store and the owning service"] is True and row["record written"]
            and "memory.record.put" in (row["audit events"] or [])
            and {"data/artifact/index.db", "memory/memory.audit.db"} <= {h["file"] for h in alive["connections"]},
            {k: row.get(k) for k in ("close calls after the build", "assembly holds the artifact store and the owning service",
                                     "audit events", "open connections while the assembly lives", "error")})
    c.check(f"{tag} assembly.close() closes each resource exactly once; a second close returns the same result",
            row.get("close results") == {"first": [True, "closed"], "second is first": True}
            and row["close calls after two assembly.close()"] == SERVICE,
            {"close results": row.get("close results"), "close calls": row["close calls after two assembly.close()"]})
    ops = row["file operations after close"]
    c.check(f"{tag} after close (GC off): no open connection; every SQLite file renames, deletes, folders go",
            row["open connections after close"]["count"] == 0 and bool(ops) and all(v == "ok" for v in ops.values()),
            {"open": row["open connections after close"], "operations": ops})

    # C02: a successful external-backend build.
    key, case = "C02", ROOT / "C02"
    case.mkdir(parents=True)
    tag = "[C02: successful external-backend assembly]"
    row = {}
    tracker.case = key
    try:
        assembly = build(case, memory_config=external_config(case))
        row["close calls after the build"] = tracker.calls(key)
        row["service store type"] = type(assembly.service._store).__name__
        row["delegated_store"] = repr(assembly.delegated_store)
        row["record written"] = bool(assembly.service.write_record(scope="agent:alpha", record_type="fact",
                                                                   title="External", content="External content"))
        first = assembly.close()
        second = assembly.close()
        row["close results"] = {"first": [first.closed, first.reason_code], "second is first": second is first}
    except Exception as exc:  # noqa: BLE001
        row["error"] = lib.err(exc)
    finally:
        tracker.case = None
    row["close calls after two assembly.close()"] = tracker.calls(key)
    row["open connections after close"] = lib.open_connections(case)
    row["file operations after close"] = lib.immediate_file_ops(case, case_folders(case))
    results[key] = row
    expected = {**AC, "external backend": [1], "BackendMemoryStoreAdapter": [1], "MemoryService (owner)": [1]}
    c.check(f"{tag} no built-in memory store, audit sink or audited store is created; the service wraps the "
            "external backend; nothing closes during the build",
            "error" not in row and row["close calls after the build"] == {k: [0] for k in expected}
            and row["service store type"] == "BackendMemoryStoreAdapter" and row["delegated_store"] == "None"
            and row["record written"] is True,
            {k: row.get(k) for k in ("close calls after the build", "service store type", "delegated_store", "error")})
    c.check(f"{tag} assembly.close() closes the external backend and the artifact store once each; "
            "a second close changes nothing; the files go at once",
            row.get("close results") == {"first": [True, "closed"], "second is first": True}
            and row["close calls after two assembly.close()"] == expected
            and row["open connections after close"]["count"] == 0
            and all(v == "ok" for v in row["file operations after close"].values()),
            {"close calls": row["close calls after two assembly.close()"], "close results": row.get("close results"),
             "operations": row["file operations after close"]})


# ------------------------------------------------------------------ PostgreSQL
def postgres_cases(admin: Any, engines: lib.Engines) -> None:
    import sqlalchemy as sa

    def pg_case(key: str, what: str, *, failing: str | None, expected: dict[str, list[int]],
                raise_after: tuple[str, ...] = ()) -> None:
        tag = f"[{key}: PostgreSQL, {what}]"
        case = ROOT / key
        case.mkdir(parents=True)
        schema = f"probe390_{key.lower()}_{uuid.uuid4().hex[:8]}"
        app = f"probe390-{key.lower()}-{uuid.uuid4().hex[:6]}"
        with admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        sep = "&" if "?" in PG_BASE else "?"
        url = f"{PG_BASE}{sep}options={quote_plus('-csearch_path=' + schema)}&application_name={app}"
        memory_config = {"store": {"backend": "postgres", "postgres": {"url": url}}}
        row: dict[str, Any] = {"what": what, "schema": schema, "application_name": app}
        seen: dict[str, Any] = {}

        def snapshot() -> None:
            seen["server connections before cleanup"] = lib.server_connections(admin, app)
            seen["engine disposals before cleanup"] = [r["disposed"] for r in engines.for_app(app)]

        holder: dict = {}
        overrides: dict[str, Any] = {"memory_config": memory_config}
        restore = None
        if failing == "store":
            restore = inject_audit_sink_failure(snapshot)
        elif failing == "audit":
            restore = inject_ranking_failure(snapshot)
        elif failing == "service":
            overrides["adapter_cls"] = raising_gateway(holder, snapshot)
        engines.app = app
        tracker.case = key
        tracker.raise_after = set(raise_after)
        caught = None
        try:
            try:
                assembly = build(case, **overrides)
                row["build"] = "returned an assembly"
                snapshot()
                rid = assembly.service.write_record(scope="agent:alpha", record_type="fact", title="PG",
                                                    content="PG content")
                with admin.connect() as conn:
                    row["server-side rows in memory_records"] = conn.execute(
                        sa.text(f'SELECT count(*) FROM "{schema}".memory_records WHERE id = :id'), {"id": rid}).scalar_one()
                first = assembly.close()
                row["assembly.close()"] = [first.closed, first.reason_code, assembly.close() is first]
            except Exception as exc:  # noqa: BLE001
                caught = describe(exc)
                if holder:
                    row["the caught exception is the build's own object"] = exc is holder.get("exc")
                row["in the handler: open sqlite3 connections on the case's files"] = lib.open_connections(case)
                row["in the handler: server connections (pg_stat_activity, polled)"] = lib.settle_connections(admin, app)
        finally:
            if restore is not None:
                restore()
            tracker.case = None
            tracker.raise_after = set()
            engines.app = None
        row["error"] = caught
        row.update(seen)
        row["close calls (per instance)"] = tracker.calls(key)
        row["engine disposals"] = [r["disposed"] for r in engines.for_app(app)]
        row["server connections after"] = lib.settle_connections(admin, app)
        with admin.connect() as conn:
            row["memory_records table in the case schema"] = conn.execute(sa.text(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = :s AND table_name = 'memory_records'"),
                {"s": schema}).scalar_one()
        # Cleanup only after every observation: dispose what the build left, drop the schema.
        for rec in engines.for_app(app):
            rec["engine"].dispose()
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        results[key] = row

        c.check(f"{tag} the store bootstrapped its schema on the server (memory_records in the case's own schema)",
                row["memory_records table in the case schema"] == 1, row["memory_records table in the case schema"])
        at = row.get("server connections before cleanup")
        if failing is None:
            c.check(f"{tag} while the assembly lives it holds a server connection and nothing is disposed; "
                    "a record it writes is read back on the server",
                    (at or 0) >= 1 and row.get("engine disposals before cleanup") == [0]
                    and row.get("server-side rows in memory_records") == 1,
                    {k: row.get(k) for k in ("server connections before cleanup", "engine disposals before cleanup",
                                             "server-side rows in memory_records", "error")})
            c.check(f"{tag} assembly.close() closes each resource once, disposes the engine once; a second close "
                    "returns the same result",
                    row.get("assembly.close()") == [True, "closed", True] and row["close calls (per instance)"] == expected
                    and row["engine disposals"] == [1],
                    {"assembly.close()": row.get("assembly.close()"), "close calls": row["close calls (per instance)"],
                     "engine disposals": row["engine disposals"]})
        else:
            c.check(f"{tag} the build raises the injected error", caught is not None and "probe:" in caught["message"],
                    caught)
            c.check(f"{tag} at the failure the owned store held a server connection (pg_stat_activity) and its "
                    "engine was not yet disposed",
                    (at or 0) >= 1 and row.get("engine disposals before cleanup") == [0],
                    {"server connections": at, "engine disposals": row.get("engine disposals before cleanup")})
            c.check(f"{tag} each resource the build owned at the failure is closed exactly once; nothing else",
                    row["close calls (per instance)"] == expected,
                    {"observed": row["close calls (per instance)"], "expected": expected})
            c.check(f"{tag} the owned engine is disposed exactly once", row["engine disposals"] == [1],
                    row["engine disposals"])
            open_sqlite = row.get("in the handler: open sqlite3 connections on the case's files") or {"count": None}
            c.check(f"{tag} right after the failure no sqlite3 connection is open on the case's files (artifact index)",
                    open_sqlite["count"] == 0, open_sqlite)
            if raise_after:
                c.check(f"{tag} the caller gets the build's own exception object although a close raised",
                        row.get("the caught exception is the build's own object") is True and caught["__context__"] is None,
                        {"same object": row.get("the caught exception is the build's own object"), **(caught or {})})
        in_handler = row.get("in the handler: server connections (pg_stat_activity, polled)")
        c.check(f"{tag} no server connection is left " + ("after close" if failing is None else
                "right after the failure (in the except block) or afterwards") + " (pg_stat_activity settles at 0)",
                row["server connections after"]["count"] == 0 and (failing is None or (in_handler or {}).get("count") == 0),
                {"in the except block": in_handler, "afterwards": row["server connections after"]})

    pg_store = {k: v for k, v in AC.items()} | {"PostgresMemoryStore": [1]}
    pg_audited = pg_store | {"SQLiteMemoryAuditSink": [1], "AuditedMemoryStore": [1]}
    pg_service = pg_audited | {"MemoryService (owner)": [1], "MemoryService (non-owning)": [0]}
    pg_case("P01", "after the store: SQLiteMemoryAuditSink raises (injected)", failing="store", expected=pg_store)
    pg_case("P02", "after the audited store: merged_ranking_config raises (injected)", failing="audit",
            expected=pg_audited)
    pg_case("P03", "after the service: adapter_cls raises", failing="service", expected=pg_service)
    pg_case("P04", "after the service: adapter_cls raises and the artifact store's close() raises",
            failing="service", expected=pg_service, raise_after=("ArtifactCtl",))
    pg_case("P05", "successful assembly lifetime (control)", failing=None, expected=pg_service)


def main() -> int:
    quiet()
    clear_override_env()
    builder = Path(rm.__file__)
    extra: dict[str, Any] = {
        "openminion_dist_version": metadata.version("openminion"),
        "builder module": str(builder),
        "builder module sha256": hashlib.sha256(builder.read_bytes()).hexdigest(),
        "builder has _MemoryBuildOwners": hasattr(rm, "_MemoryBuildOwners"),
        "gc enabled": gc.isenabled(),
        "postgres_requested": POSTGRES,
    }
    admin = engines = None
    if POSTGRES and PG_BASE:
        import psycopg
        import sqlalchemy as sa

        admin = sa.create_engine(PG_BASE, future=True)
        with admin.connect() as conn:
            extra["postgres_version"] = conn.execute(sa.text("SELECT version()")).scalar_one()
        extra["sqlalchemy"] = sa.__version__
        extra["psycopg"] = psycopg.__version__
        engines = lib.Engines()
    elif POSTGRES:
        extra["postgres"] = "NOT RUN: OPENMINION_TEST_POSTGRES_URL is not set"
    info = banner("probe_390", **extra)
    c.check("openminion is imported from site-packages (installed package, not a checkout), isolated mode",
            info["imported_from_site_packages"] is True and info["isolated_mode"] is True,
            {"openminion.__file__": info["openminion.__file__"], "version": info["openminion.__version__"]})
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    for name, step in (("SQLite and built-in store cases", sqlite_cases), ("success controls", success_cases)):
        print(f"== {name}", flush=True)
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - a probe error is a failed check, not a crash
            c.check(f"{name} ran to the end", False, lib.err(exc))
    if POSTGRES:
        print("== PostgreSQL cases", flush=True)
        if admin is None:
            c.check("postgres requested but OPENMINION_TEST_POSTGRES_URL is not set (a skip is not a pass)", False, "not run")
        else:
            try:
                postgres_cases(admin, engines)
            except Exception as exc:  # noqa: BLE001
                c.check("PostgreSQL cases ran to the end", False, lib.err(exc))
            admin.dispose()
    c.check("cyclic GC stayed off for the whole probe and gc.collect was never called",
            not gc.isenabled() and GC_CALLS["n"] == 0, {"enabled": gc.isenabled(), "gc.collect calls": GC_CALLS["n"]})
    results["working directory entries at the end"] = sorted(p.name for p in Path.cwd().iterdir())
    summary = c.summary()
    payload = {"info": info, "results": results, "checks": c.items, "summary": summary}
    text = sanitize(payload)
    if PG_BASE:
        text = lib_redact(text)
    write_result(OUT, text)
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    return 0 if c.all_pass else 1


def lib_redact(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace(PG_BASE, "<PGURL>")
    if isinstance(value, dict):
        return {lib_redact(k): lib_redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [lib_redact(v) for v in value]
    return value


if __name__ == "__main__":
    sys.exit(main())
