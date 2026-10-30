"""#447 on the installed package: a failed APIRuntime start closes what it built.

    python -I -X utf8 probe_447.py <work_dir> <out.json> [--backend all|sqlite|postgres]
                                   [--baseline <0.1.15 probe_447.json>] [--cases a,b,...]

Every case runs in its own child process (this file with --child), so threads,
handles and connections from one case cannot show up in another. The child
disables automatic garbage collection before it imports openminion and replaces
gc.collect with a counting wrapper, so the snapshot right after the exception (or
after close() for the controls) shows what the owners closed, not what a
collection freed. The child keeps the exception it caught (with its traceback)
until the snapshot, as a caller handling it would. The driver turns each child's
observations into checks named after the maintainer's checklist items.

Runtime cases: one offline `echo` agent, `APIRuntime.from_config_path`, SQLite
runtime storage, or PostgreSQL (a new database per case, needs PGURL). The
counted closes are the existing close methods of each owner (the same list as
the PR's tests); the step that fails is injected with the error instance the
caller must receive, or comes from the configuration (memory-provider,
sessions-table). Snapshot: open files under the runtime home (psutil), the start
threads still alive (runtimectl-*, storage-pool-health-emitter), on PostgreSQL the
server connections to the case database (pg_stat_activity, from a separate
admin connection, waiting up to 5 s for a disconnect to arrive), and on Windows
whether every file under the home can be renamed and the home removed at once.

StorageEngine cases (item 3): StorageEngine.from_config with counted record and
vector backends (the record store is the real SQLite or PostgreSQL one), a
backend whose close raises, one object registered as both backends, a caller's
TelemetryService borrowed by build_runtime_storage, and the successful
StorageEngine.close() for comparison with 0.1.15 (--baseline).
"""

from __future__ import annotations

import gc

gc.disable()
_REAL_COLLECT = gc.collect
GC_CALLS: list[str] = []


def _counting_collect(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    import traceback as _tb

    GC_CALLS.append(" <- ".join(f"{f.name}" for f in _tb.extract_stack()[-4:-1]))
    return _REAL_COLLECT(*args, **kwargs)


gc.collect = _counting_collect  # type: ignore[assignment]

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from collections import Counter  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402
from urllib.parse import urlsplit, urlunsplit  # noqa: E402

PGURL = (os.environ.get("PGURL") or os.environ.get("OPENMINION_TEST_POSTGRES_URL") or "").strip()
STRIP_ENV = (
    "OPENMINION_HOME", "OPENMINION_DATA_ROOT", "OPENMINION_GENERATED_ROOT", "OPENMINION_CONFIG_PATH",
    "OPENMINION_STORAGE_BACKEND", "OPENMINION_STORAGE_POSTGRES_URL", "OPENMINION_MEMORY_PROVIDER",
)
START_THREADS = ("runtimectl-", "storage-pool-health-emitter")
PROMPT = "hello from probe_447"

# What each step adds to the owners that have something to close (the PR's tests).
_STORAGE = {"record_store", "telemetry_service"}
_ENGINE = _STORAGE | {"storage_engine"}
_RUNTIME_STORAGE = _ENGINE | {"runtime_storage"}
_OPS = _RUNTIME_STORAGE | {"llm_runtime", "action_policy"}
_RETRIEVE = _OPS | {"ops_service", "retrieve_ctl"}
_MEMORY = _RETRIEVE | {"memory_assembly"}
_SUPPORT = _MEMORY | {"authored_tools"}
_AGENT = _SUPPORT | {"agent"}
_INFRA_AND_STACK = _AGENT | {"gateway"}
_RUNTIME = _INFRA_AND_STACK | {"runtime"}
_STARTED = _RUNTIME | {"runtime_manager"}

# case: (checklist item, what fails, expected owners closed, expected error ("injected" or type name), backends)
RUNTIME_CASES: dict[str, tuple[int, str, set[str], str | None, tuple[str, ...]]] = {
    "control": (1, "none: start, one SDK turn, close()", _STARTED, None, ("sqlite", "postgres")),
    "engine-emitter": (2, "the pool-health emitter start inside StorageEngine.from_config (before construction)",
                       _STORAGE, "injected", ("sqlite", "postgres")),
    "storage-migration": (2, "the runtime migration inside build_runtime_storage", _ENGINE, "injected",
                          ("sqlite", "postgres")),
    "extensions": (2, "the extension runtime build", _RUNTIME_STORAGE, "injected", ("sqlite", "postgres")),
    "ops": (2, "the ops service, after the action policy exists", _OPS, "injected", ("sqlite", "postgres")),
    "support-authoring": (2, "tool authoring, after the memory assembly started", _MEMORY, "injected",
                          ("sqlite", "postgres")),
    "after-support": (2, "the channel authenticity policy, after the support part", _SUPPORT, "injected",
                      ("sqlite", "postgres")),
    "gateway": (2, "the gateway build, after the agent exists", _AGENT, "injected", ("sqlite", "postgres")),
    "connection": (2, "the SQLite connection read in the runtime constructor's arguments", _INFRA_AND_STACK,
                   "injected", ("sqlite",)),
    "post-init": (2, "APIRuntime.__post_init__ (initialize_runtime_components)", _INFRA_AND_STACK, "injected",
                  ("sqlite", "postgres")),
    "runtime-manager": (2, "build_runtime_manager, after the runtime object exists (ownership handoff)", _RUNTIME,
                        "injected", ("sqlite", "postgres")),
    "bind": (2, "the runtime handle bind, after the runtime manager started (ownership handoff)", _STARTED,
             "injected", ("sqlite", "postgres")),
    "close-fails-gateway": (3, "the gateway build, and RuntimeStorageContext.close raises after closing", _AGENT,
                            "injected", ("sqlite", "postgres")),
    "close-fails-bind": (3, "the runtime handle bind, and RuntimeStorageContext.close raises after closing",
                         _STARTED, "injected", ("sqlite", "postgres")),
    "memory-provider": (4, "OPENMINION_MEMORY_PROVIDER=not-a-provider (ConfigError, not injected)", _RETRIEVE,
                        "ConfigError", ("sqlite", "postgres")),
    "sessions-table": (4, "the database already has a sessions table (MigrationError at v1, not injected)", _ENGINE,
                       "MigrationError", ("sqlite", "postgres")),
}
ENGINE_CASES: dict[str, tuple[str, tuple[str, ...]]] = {
    "engine-blob-fails": ("StorageEngine.from_config: the blob store fails before the vector store exists",
                          ("sqlite", "postgres")),
    "engine-vector-close-fails": ("StorageEngine.from_config: the emitter start fails and the vector store's close "
                                  "raises", ("sqlite", "postgres")),
    "engine-record-close-fails": ("StorageEngine.from_config: the emitter start fails and the record store's close "
                                  "raises", ("sqlite", "postgres")),
    "engine-aliased": ("StorageEngine.from_config: one object is both the record and the vector store, the emitter "
                       "start fails", ("sqlite",)),
    "storage-borrowed-telemetry": ("build_runtime_storage with the caller's TelemetryService behind the storage "
                                   "hook: the runtime migration fails", ("sqlite", "postgres")),
    "storage-borrowed-telemetry-close-fails": ("the same, and StorageEngine.close raises after closing",
                                               ("sqlite", "postgres")),
    "engine-normal-close": ("a successful StorageEngine.from_config, then StorageEngine.close()",
                            ("sqlite", "postgres")),
}


class Injected(RuntimeError):
    """The error a case injects at one step."""


# ---------------------------------------------------------------- PostgreSQL helpers
def pg_url(database: str) -> str:
    return urlunsplit(urlsplit(PGURL)._replace(path=f"/{database}"))


def _admin_engine():
    import sqlalchemy as sa
    from sqlalchemy.pool import NullPool

    return sa.create_engine(pg_url("postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT")


def pg_admin(sql: str) -> None:
    import sqlalchemy as sa

    engine = _admin_engine()
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(sql))
    finally:
        engine.dispose()


def pg_connections(database: str) -> int:
    import sqlalchemy as sa

    engine = _admin_engine()
    try:
        with engine.connect() as conn:
            return int(conn.execute(sa.text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = :d AND pid <> pg_backend_pid()"
            ), {"d": database}).scalar_one())
    finally:
        engine.dispose()


def pg_connections_settled(database: str, timeout: float = 5.0) -> dict[str, Any]:
    start = time.monotonic()
    while True:
        count = pg_connections(database)
        waited = round(time.monotonic() - start, 2)
        if count == 0 or waited >= timeout:
            return {"count": count, "waited_s": waited}
        time.sleep(0.05)


def pg_rows(database: str, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    import sqlalchemy as sa
    from sqlalchemy.pool import NullPool

    engine = sa.create_engine(pg_url(database), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(sa.text(sql), params or {})]
    finally:
        engine.dispose()


def pg_execute(database: str, sql: str) -> None:
    import sqlalchemy as sa
    from sqlalchemy.pool import NullPool

    engine = sa.create_engine(pg_url(database), poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(sql))
    finally:
        engine.dispose()


# ---------------------------------------------------------------- snapshots
def open_files_under(root: Path) -> list[str]:
    import psutil

    prefix = os.path.normcase(str(root.resolve()))
    held = set()
    for item in psutil.Process().open_files():
        path = os.path.normcase(str(Path(item.path).resolve()))
        if path.startswith(prefix):
            held.add(Path(path).relative_to(prefix).as_posix())
    return sorted(held)


def start_threads_alive(before: set[threading.Thread]) -> list[str]:
    return sorted(
        t.name for t in threading.enumerate()
        if t not in before and t.is_alive() and t.name.startswith(START_THREADS)
    )


def snapshot(root: Path, before: set[threading.Thread], database: str | None) -> dict[str, Any]:
    files = open_files_under(root)
    out: dict[str, Any] = {
        "open_files": files,
        "open_databases": [f for f in files if f.endswith((".db", ".sqlite", ".sqlite3"))],
        "start_threads_alive": start_threads_alive(before),
        "gc_enabled": gc.isenabled(),
        "gc_collect_calls_so_far": list(GC_CALLS),
    }
    if database:
        out["pg_connections"] = pg_connections_settled(database)
    return out


def rename_and_remove(root: Path) -> dict[str, Any]:
    """Rename every file under root and back, then remove root (Windows: an open handle blocks both)."""
    files = sorted(p for p in root.rglob("*") if p.is_file())
    failed = []
    for path in files:
        moved = path.with_name(path.name + ".moved")
        try:
            os.replace(path, moved)
        except OSError as exc:
            failed.append(f"{path.relative_to(root).as_posix()}: {type(exc).__name__} [WinError {getattr(exc, 'winerror', '?')}]")
            continue
        os.replace(moved, path)
    out: dict[str, Any] = {
        "files": len(files),
        "database_files": sorted(p.relative_to(root).as_posix() for p in files if p.name.endswith((".db", ".sqlite", ".db-wal", ".db-shm"))),
        "not_renamable": failed,
    }
    try:
        shutil.rmtree(root)
        out["removed"] = True
    except OSError as exc:
        out["removed"] = False
        out["remove_error"] = f"{type(exc).__name__} [WinError {getattr(exc, 'winerror', '?')}]: {exc}"
    return out


def describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}"


# ---------------------------------------------------------------- child: runtime cases
def child_runtime(case: str, backend: str, root: Path, label: str) -> dict[str, Any]:
    item, what, _owners, expected, _backends = RUNTIME_CASES[case]
    for key in STRIP_ENV:
        os.environ.pop(key, None)
    root.mkdir(parents=True, exist_ok=True)
    data_root = root / ".openminion"
    result: dict[str, Any] = {"case": case, "backend": backend, "item": item, "fails": what}
    database = None
    storage: dict[str, Any] = {"path": str(data_root / "state.db")}
    if backend == "postgres":
        database = f"om447_{label}_{case}".replace("-", "_")[:60]
        pg_admin(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        pg_admin(f'CREATE DATABASE "{database}"')
        storage.update(backend="postgres", postgres_url=pg_url(database))
        result["database"] = database
    (root / "agents.json").write_text(json.dumps({
        "default_agent": "first",
        "agents": {"first": {"provider": "echo", "default_channel": "console"}},
        "runtime": {"process_mode": "single-process", "daemon_auto_start": False},
        "storage": storage,
    }, indent=2) + "\n", encoding="utf-8")
    if case == "sessions-table":
        if database:
            pg_execute(database, "CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
        else:
            data_root.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(data_root / "state.db")
            conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
            conn.commit()
            conn.close()
        result["setup"] = "CREATE TABLE sessions (session_id TEXT PRIMARY KEY)"
    if case == "memory-provider":
        os.environ["OPENMINION_MEMORY_PROVIDER"] = "not-a-provider"
        result["setup"] = "OPENMINION_MEMORY_PROVIDER=not-a-provider"

    import openminion
    import openminion.api.core.bootstrap as bootstrap
    import openminion.api.core.infrastructure as infrastructure
    import openminion.api.runtime as runtime_module
    import openminion.modules.storage.engine as engine_module
    import openminion.modules.storage.runtime.context as storage_context
    from openminion import APIRuntime
    from openminion.modules.llm.providers.factory import RuntimeLLMHandle
    from openminion.modules.memory.runtime.assembly import RuntimeMemoryAssembly
    from openminion.modules.policy.runtime.service import PolicyCtl
    from openminion.modules.retrieve.runtime.retrieve import RetrieveCtl
    from openminion.modules.storage.engine import StorageEngine
    from openminion.modules.storage.record_store import RecordStoreSQLite
    from openminion.modules.telemetry.service import TelemetryService
    from openminion.modules.tool.authoring import ToolAuthoringService
    from openminion.services.agent.service import AgentService
    from openminion.services.gateway.service import GatewayService
    from openminion.services.runtime.manager import AgentRuntimeManager
    from openminion.tools.ops.service import OpsService

    result["openminion"] = getattr(openminion, "__version__", "?")
    counted = [
        (storage_context.RuntimeStorageContext, "close", "runtime_storage"),
        (StorageEngine, "close", "storage_engine"),
        (RecordStoreSQLite, "close", "record_store"),
        (TelemetryService, "close_sync", "telemetry_service"),
        (PolicyCtl, "close", "action_policy"),
        (OpsService, "close", "ops_service"),
        (RetrieveCtl, "close", "retrieve_ctl"),
        (RuntimeMemoryAssembly, "close", "memory_assembly"),
        (ToolAuthoringService, "close", "authored_tools"),
        (RuntimeLLMHandle, "close", "llm_runtime"),
        (AgentService, "close", "agent"),
        (GatewayService, "close", "gateway"),
        (APIRuntime, "close", "runtime"),
        (AgentRuntimeManager, "shutdown", "runtime_manager"),
    ]
    if backend == "postgres":
        from openminion.modules.storage.backends.postgres import RecordStorePostgres

        counted.append((RecordStorePostgres, "close", "record_store"))
    calls: list[tuple[str, object]] = []
    close_failures: list[str] = []

    def count(owner: Any, name: str, label_: str, *, fail: bool = False) -> None:
        original = getattr(owner, name)

        def wrapper(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            calls.append((label_, self))  # keeps the object, so its id stays unique
            out = original(self, *args, **kwargs)
            if fail:
                close_failures.append(label_)
                raise OSError(f"close of {label_} failed (injected)")
            return out

        setattr(owner, name, wrapper)

    for owner, name, label_ in counted:
        count(owner, name, label_, fail=(label_ == "runtime_storage" and case.startswith("close-fails")))

    injected = Injected(f"injected failure: {case}")

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise injected

    targets: list[tuple[Any, str]] = []
    if case == "engine-emitter":
        targets = [(engine_module, "_start_pool_health_emitter")]
    elif case == "storage-migration":
        targets = [(storage_context, "migrate_database"), (storage_context, "migrate_record_store")]
    elif case == "extensions":
        targets = [(infrastructure.LifecycleService, "build")]
    elif case == "ops":
        targets = [(infrastructure, "configured_ops_service")]
    elif case == "support-authoring":
        targets = [(infrastructure, "build_tool_authoring_service")]
    elif case == "after-support":
        targets = [(infrastructure, "build_channel_authenticity_policy")]
    elif case in ("gateway", "close-fails-gateway"):
        targets = [(bootstrap, "build_gateway_service")]
    elif case == "connection":
        targets = [(storage_context.RuntimeStorageContext, "connection")]
    elif case == "post-init":
        targets = [(runtime_module, "initialize_runtime_components")]
    elif case == "runtime-manager":
        targets = [(bootstrap, "build_runtime_manager")]
    elif case in ("bind", "close-fails-bind"):
        targets = [(APIRuntime, "_bind_runtime_handle")]
    result["patch_targets"] = {f"{getattr(o, '__name__', type(o).__name__)}.{n}": hasattr(o, n) for o, n in targets}
    for owner, name in targets:
        if name == "connection":
            setattr(owner, name, property(boom))
        elif name == "_bind_runtime_handle":
            setattr(owner, name, staticmethod(boom))
        else:
            setattr(owner, name, boom)

    threads_before = set(threading.enumerate())
    runtime = None
    caught: BaseException | None = None
    try:
        runtime = APIRuntime.from_config_path(str(root / "agents.json"), home_root=root, data_root=data_root)
        result["start"] = "ok"
    except BaseException as exc:  # noqa: BLE001 - the probe reports it
        caught = exc  # kept, with its traceback, until after the snapshot
        result["start"] = describe(exc)
        result["original_exception"] = (exc is injected) if expected == "injected" else (type(exc).__name__ == expected)
    if runtime is not None:
        if case == "control":
            from openminion import Agent

            try:
                result["handoff"] = {
                    "closes_during_start": len(calls),
                    "agent_services_hold_runtime_agent": list(getattr(runtime, "_agent_services", {}).values()) == [runtime.agent],
                    "gateways_hold_runtime_gateway": list(getattr(runtime, "_gateways", {}).values()) == [runtime.gateway],
                    "memory_assemblies_hold_runtime_assembly": list(getattr(runtime, "_memory_assemblies", {}).values())
                    == [runtime.runtime_memory_assembly],
                    "runtime_manager": type(runtime.runtime_manager).__name__,
                    "agent_runtime_handle_is_runtime": getattr(runtime.agent, "_runtime_handle", None) is runtime,
                    "start_threads_while_running": start_threads_alive(threads_before),
                }
            except Exception as exc:  # noqa: BLE001
                result["handoff"] = {"error": describe(exc)}
            try:
                with Agent(runtime=runtime) as agent:
                    reply = agent.run(PROMPT)
                result["turn"] = {"run_state": reply.run_state, "text": reply.text, "session_id": str(reply.session_id)}
            except Exception as exc:  # noqa: BLE001
                result["turn"] = {"error": describe(exc)}
        runtime.close()
        result["runtime_closed_by_probe"] = True
        runtime = None
    per_object = Counter((label_, id(target)) for label_, target in calls)
    result["closes"] = dict(sorted(Counter(label_ for label_, _ in calls).items()))
    result["closes_per_object"] = sorted(f"{label_}#{n}" for (label_, _), n in per_object.items())
    result["max_per_object"] = {
        label_: max(n for (other, _), n in per_object.items() if other == label_) for label_ in sorted(result["closes"])
    }
    result["injected_close_failures"] = close_failures
    result["snapshot"] = snapshot(root, threads_before, database)
    if case == "control":
        sid = (result.get("turn") or {}).get("session_id")
        rows: dict[str, Any] = {}
        if sid:
            sql = {
                "sessions": "SELECT count(*) AS n FROM sessions WHERE id = {p}",
                "messages": "SELECT count(*) AS n FROM messages WHERE session_id = {p}",
                "room_participants": "SELECT count(*) AS n FROM room_participants WHERE session_id = {p}",
            }
            if database:
                for key, q in sql.items():
                    rows[key] = pg_rows(database, q.format(p=":s"), {"s": sid})[0]["n"]
                rows["state_db_exists"] = (data_root / "state.db").exists()
            else:
                conn = sqlite3.connect(data_root / "state.db")
                try:
                    for key, q in sql.items():
                        rows[key] = conn.execute(q.format(p="?"), (sid,)).fetchone()[0]
                finally:
                    conn.close()
        result["persisted_rows"] = rows
    if os.name == "nt":
        result["rename_and_remove"] = rename_and_remove(root)
    del caught
    _REAL_COLLECT()
    result["after_gc_collect_by_probe"] = {
        k: v for k, v in snapshot(root, threads_before, database).items() if k != "gc_collect_calls_so_far"
    } if root.exists() else "runtime home removed"
    if database:
        pg_admin(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    return result


# ---------------------------------------------------------------- child: StorageEngine cases
def child_engine(case: str, backend: str, root: Path, label: str) -> dict[str, Any]:
    for key in STRIP_ENV:
        os.environ.pop(key, None)
    root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"case": case, "backend": backend, "item": 3, "what": ENGINE_CASES[case][0]}
    database = None
    if backend == "postgres":
        database = f"om447_{label}_{case}".replace("-", "_")[:60]
        pg_admin(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        pg_admin(f'CREATE DATABASE "{database}"')
        result["database"] = database

    import openminion
    from openminion.modules.storage import StorageEngine, StorageEngineConfig, default_backend_registry
    from openminion.modules.storage import engine as storage_engine
    from openminion.modules.storage.record_store import RecordStoreSQLite
    from openminion.modules.storage.runtime import context as storage_context
    from openminion.modules.storage.runtime.context import build_runtime_storage
    from openminion.modules.telemetry import storage_hook
    from openminion.modules.telemetry.service import TelemetryService

    result["openminion"] = getattr(openminion, "__version__", "?")
    version = getattr(openminion, "__version__", "0")
    real_id = "record.postgres" if database else "record.sqlite"
    closes: list[str] = []
    stores: list[Any] = []
    injected = Injected(f"injected failure: {case}")

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise injected

    def counted_record(options: dict[str, Any], *, fail_close: bool = False) -> Any:
        store = default_backend_registry().create_record(real_id, options)
        original = store.close

        def close() -> None:
            closes.append("record")
            original()
            if fail_close:
                raise OSError("record store close failed (injected)")

        store.close = close  # instance attribute: the engine's close finds it
        if database:
            store.healthcheck()  # a pooled server connection is open before the failure
        stores.append(store)
        return store

    class VectorStore:
        contract_version = "v1"

        def __init__(self, fail_close: bool = False) -> None:
            self.fail_close = fail_close

        def upsert(self, vectors, metadata, ids, namespace=None):  # noqa: ANN001
            return None

        def search(self, query_vector, top_k=10, filters=None, namespace=None):  # noqa: ANN001
            return []

        def delete(self, ids, namespace=None) -> bool:  # noqa: ANN001
            return True

        def list_namespaces(self) -> list[str]:
            return []

        def namespace_stats(self, namespace: str) -> dict[str, Any]:
            return {"namespace": namespace, "count": 0}

        def count(self, namespace=None) -> int:  # noqa: ANN001
            return 0

        def healthcheck(self) -> dict[str, Any]:
            return {"ok": True}

        def describe_backend(self) -> dict[str, Any]:
            return {"backend_id": "vector.counted", "version": version, "planes_supported": {"vector"},
                    "capabilities": {}, "limits": {}}

        def close(self) -> None:
            closes.append("vector")
            if self.fail_close:
                raise OSError("vector store close failed (injected)")

    class RecordAndVector(RecordStoreSQLite):
        upsert = VectorStore.upsert
        search = VectorStore.search
        delete = VectorStore.delete
        list_namespaces = VectorStore.list_namespaces
        namespace_stats = VectorStore.namespace_stats
        count = VectorStore.count
        describe_backend = VectorStore.describe_backend

        def close(self) -> None:
            closes.append("record")
            super().close()

    def engine_config(sub: str, vector: str | None) -> Any:
        base = root / sub
        return StorageEngineConfig(
            root_dir=base / "blob", sqlite_path=base / "storage.db", fallback_root=base / "fallback",
            record_backend="record.counted", vector_backend=vector,
            record_backend_options={"url": pg_url(database)} if database else {},
        )

    def record_state() -> dict[str, Any]:
        out: dict[str, Any] = {}
        for store in stores:
            conn = getattr(store, "_conn", None) if not database else None
            if isinstance(conn, sqlite3.Connection):
                try:
                    conn.execute("SELECT 1")
                    out.setdefault("sqlite_connections_open", []).append(True)
                except sqlite3.ProgrammingError:
                    out.setdefault("sqlite_connections_open", []).append(False)
        if database:
            out["pg_connections"] = pg_connections_settled(database)
        return out

    threads_before = set(threading.enumerate())
    if case in ("engine-blob-fails", "engine-vector-close-fails", "engine-record-close-fails", "engine-aliased"):
        registry = default_backend_registry()
        vector_id = None
        if case == "engine-aliased":
            shared = RecordAndVector(root / "shared" / "storage.db")
            stores.append(shared)
            registry.register_record("record.counted", lambda options: shared)
            registry.register_vector("vector.shared", lambda options: shared)
            vector_id = "vector.shared"
        else:
            registry.register_record("record.counted", lambda options: counted_record(
                options, fail_close=(case == "engine-record-close-fails")))
            if case != "engine-record-close-fails":
                registry.register_vector("vector.counted", lambda options: VectorStore(
                    fail_close=(case == "engine-vector-close-fails")))
                vector_id = "vector.counted"
        if case == "engine-blob-fails":
            registry.register_blob("blob.fs", boom)
            result["patch_targets"] = {"BackendRegistry.register_blob": True}
        else:
            result["patch_targets"] = {"engine._start_pool_health_emitter": hasattr(storage_engine, "_start_pool_health_emitter")}
            original_start = storage_engine._start_pool_health_emitter

            def failing_start(*args: Any, **kwargs: Any) -> Any:
                if database:
                    result["pg_connections_at_failure_point"] = pg_connections(database)
                raise injected

            storage_engine._start_pool_health_emitter = failing_start
            del original_start
        (root / "e").mkdir(parents=True, exist_ok=True)
        caught = None
        try:
            StorageEngine.from_config(config=engine_config("e", vector_id), registry=registry)
            result["start"] = "ok"
        except BaseException as exc:  # noqa: BLE001
            caught = exc
            result["start"] = describe(exc)
            result["original_exception"] = exc is injected
        result["closes"] = list(closes)
        result["record_state"] = record_state()
        result["start_threads_alive"] = start_threads_alive(threads_before)
        result["gc_collect_calls_so_far"] = list(GC_CALLS)
        result["gc_enabled"] = gc.isenabled()
        del caught
    elif case.startswith("storage-borrowed-telemetry"):
        engines: list[Any] = []
        engine_close = StorageEngine.close
        from_config = StorageEngine.from_config.__func__  # type: ignore[attr-defined]
        telemetry_closes: list[Any] = []
        telemetry_close = TelemetryService.close_sync
        fail_engine_close = case.endswith("close-fails")

        def recording_from_config(cls, **kwargs: Any) -> Any:  # noqa: ANN001
            engines.append(from_config(cls, **kwargs))
            if database:
                result["pg_connections_with_engine_open"] = pg_connections(database)
                emitter = getattr(engines[-1], "_pool_health_emitter", None)
                result["pool_health_emitter_started"] = bool(emitter is not None and emitter.is_running)
            return engines[-1]

        def counted_engine_close(self) -> None:  # noqa: ANN001
            closes.append("storage_engine")
            engine_close(self)
            if fail_engine_close:
                raise OSError("engine close failed (injected)")

        def counted_telemetry_close(self) -> Any:  # noqa: ANN001
            telemetry_closes.append("telemetry_service")
            return telemetry_close(self)

        StorageEngine.from_config = classmethod(recording_from_config)  # type: ignore[assignment]
        StorageEngine.close = counted_engine_close  # type: ignore[assignment]
        TelemetryService.close_sync = counted_telemetry_close  # type: ignore[assignment]
        storage_context.migrate_database = boom
        storage_context.migrate_record_store = boom
        result["patch_targets"] = {"context.migrate_database": True, "context.migrate_record_store": True}
        telemetry = TelemetryService(home_root=root / "telemetry-home")
        kwargs: dict[str, Any] = {"env": {}, "telemetry_hook": storage_hook.TelemetryServiceStorageHook(telemetry)}
        if database:
            kwargs.update(record_backend="record.postgres", record_backend_options={"url": pg_url(database)})
        caught = None
        try:
            build_runtime_storage(root / "rs" / "state.db", **kwargs)
            result["start"] = "ok"
        except BaseException as exc:  # noqa: BLE001
            caught = exc
            result["start"] = describe(exc)
            result["original_exception"] = exc is injected
        result["closes"] = list(closes)
        result["engines_built"] = len(engines)
        if engines and not database:
            conn = getattr(engines[0].record_store, "_conn", None) or getattr(engines[0].record_store, "connection", None)
            try:
                conn.execute("SELECT 1")
                result["record_sqlite_connection_open"] = True
            except sqlite3.ProgrammingError:
                result["record_sqlite_connection_open"] = False
        if database:
            result["pg_connections"] = pg_connections_settled(database)
        result["start_threads_alive"] = start_threads_alive(threads_before)
        result["telemetry_close_sync_calls_by_storage"] = len(telemetry_closes)
        result["open_files_before_caller_close"] = open_files_under(root)
        telemetry.close_sync()  # the caller, which owns it, closes it
        result["telemetry_close_sync_calls_after_caller_close"] = len(telemetry_closes)
        result["open_files_after_caller_close"] = open_files_under(root)
        result["gc_collect_calls_so_far"] = list(GC_CALLS)
        result["gc_enabled"] = gc.isenabled()
        del caught
        engines.clear()
    elif case == "engine-normal-close":
        observed: dict[str, Any] = {}
        hook_telemetry = TelemetryService(home_root=root / "telemetry-home") if database else None
        # 1. separate record and vector stores; with PostgreSQL the storage hook starts the pool-health emitter
        registry = default_backend_registry()
        registry.register_record("record.counted", lambda options: counted_record(options))
        registry.register_vector("vector.counted", lambda options: VectorStore())
        engine = StorageEngine.from_config(
            config=engine_config("n1", "vector.counted"), registry=registry,
            telemetry_hook=storage_hook.TelemetryServiceStorageHook(hook_telemetry) if hook_telemetry else None,
        )
        emitter = getattr(engine, "_pool_health_emitter", None)
        sep: dict[str, Any] = {"emitter_running_before_close": bool(emitter is not None and emitter.is_running)}
        if database:
            sep["pg_connections_before_close"] = pg_connections(database)
        del closes[:]
        engine.close()
        sep["closes"] = list(closes)
        sep["emitter_running_after_close"] = bool(emitter is not None and emitter.is_running)
        sep["start_threads_alive"] = start_threads_alive(threads_before)
        sep.update(record_state())
        observed["separate_stores"] = sep
        engine = emitter = None
        stores.clear()
        # 2. one object as both stores (SQLite)
        if not database:
            shared = RecordAndVector(root / "n2" / "storage.db")
            registry = default_backend_registry()
            registry.register_record("record.counted", lambda options: shared)
            registry.register_vector("vector.shared", lambda options: shared)
            engine = StorageEngine.from_config(config=engine_config("n2", "vector.shared"), registry=registry)
            del closes[:]
            engine.close()
            try:
                shared.connection.execute("SELECT 1")
                shared_open = True
            except sqlite3.ProgrammingError:
                shared_open = False
            observed["aliased_store"] = {"closes": list(closes), "connection_open": shared_open}
            engine = shared = None
        # 3. the vector store's close raises during close()
        registry = default_backend_registry()
        registry.register_record("record.counted", lambda options: counted_record(options))
        registry.register_vector("vector.counted", lambda options: VectorStore(fail_close=True))
        engine = StorageEngine.from_config(config=engine_config("n3", "vector.counted"), registry=registry)
        del closes[:]
        try:
            engine.close()
            raising = "returned"
        except Exception as exc:  # noqa: BLE001
            raising = describe(exc)
        state = record_state()
        observed["vector_close_raises"] = {"close()": raising, "closes": list(closes),
                                           "record_store_open_after": state}
        for store in stores:  # probe cleanup after the observation
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass
        stores.clear()
        engine = None
        if hook_telemetry is not None:
            hook_telemetry.close_sync()
        if database:
            observed["pg_connections_after_cleanup"] = pg_connections_settled(database)["count"]
        result["observed"] = observed
        result["gc_collect_calls_so_far"] = list(GC_CALLS)
        result["gc_enabled"] = gc.isenabled()
    if database:
        pg_admin(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    return result


# ---------------------------------------------------------------- driver
def run_child(case: str, backend: str, work: Path, label: str) -> dict[str, Any]:
    name = f"{backend}-{case}"
    root = work / name
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    out = work / f"{name}.json"
    proc = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve()), "--child", case, "--backend", backend,
         "--root", str(root), "--child-out", str(out), "--label", label],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )
    try:
        result = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result = {"probe_error": f"child produced no JSON: {exc}"}
    result["child_exit_code"] = proc.returncode
    if proc.returncode != 0 or "probe_error" in result:
        result["child_stderr_tail"] = proc.stderr[-4000:]
    return result


def evaluate_runtime(checks: Any, case: str, backend: str, r: dict[str, Any], base: dict[str, Any] | None) -> None:
    item, what, owners, expected, _ = RUNTIME_CASES[case]
    tag = f"item {item}: {case} ({backend})"
    if "probe_error" in r:
        checks.check(f"{tag}: the case ran", False, observed={k: r.get(k) for k in ("probe_error", "child_stderr_tail")})
        return
    snap = r["snapshot"]
    if r.get("patch_targets"):
        checks.check(f"{tag}: the injection point exists in this package ({what})", all(r["patch_targets"].values()),
                     observed=r["patch_targets"])
    if case == "control":
        turn = r.get("turn") or {}
        checks.check(f"{tag}: the start succeeds and one SDK turn (Agent(runtime=...).run) completes",
                     r.get("start") == "ok" and turn.get("run_state") == "completed",
                     observed={"start": r.get("start"), "turn": turn})
        h = r.get("handoff") or {}
        checks.check(f"{tag}: a successful start closes nothing and hands the agent, gateway, memory assembly and "
                     f"runtime manager to the runtime",
                     h.get("closes_during_start") == 0 and h.get("agent_services_hold_runtime_agent") is True
                     and h.get("gateways_hold_runtime_gateway") is True
                     and h.get("memory_assemblies_hold_runtime_assembly") is True
                     and h.get("runtime_manager") == "AgentRuntimeManager"
                     and h.get("agent_runtime_handle_is_runtime") is True, observed=h)
        rows = r.get("persisted_rows") or {}
        checks.check(f"{tag}: the turn's session, its 2 messages and its owner participant are in the runtime "
                     f"{'PostgreSQL database' if backend == 'postgres' else 'SQLite database'}",
                     rows.get("sessions") == 1 and rows.get("messages") == 2 and rows.get("room_participants") == 1,
                     observed=rows)
        when = "after close()"
    else:
        checks.check(f"{tag}: the caller receives the original failure"
                     f"{' (the injected instance)' if expected == 'injected' else f' ({expected})'}",
                     r.get("original_exception") is True, observed=r.get("start"))
        when = "right after the exception"
    labels = set(r.get("closes") or {})
    per = Counter()
    for entry in r.get("closes_per_object", []):
        lab, n = entry.rsplit("#", 1)
        per[lab] = max(per[lab], int(n))
    once = all(n == (2 if (lab == "llm_runtime" and "agent" in owners) else 1) for lab, n in per.items())
    checks.check(
        f"{tag}: each owner that had built something closed it with its existing close, each object once "
        f"(the LLM handle twice once the agent exists, as on a normal close)"
        + ("; the closes after the failing runtime-storage close still ran" if case.startswith("close-fails") else ""),
        labels == owners and once,
        observed={"closes": r.get("closes"), "max_per_object": r.get("max_per_object"),
                  "injected_close_failures": r.get("injected_close_failures")},
        expected=sorted(owners),
    )
    checks.check(f"{tag}: no garbage collection before the snapshot (automatic gc off, gc.collect not called)",
                 snap["gc_enabled"] is False and snap["gc_collect_calls_so_far"] == [],
                 observed={"gc_enabled": snap["gc_enabled"], "gc_collect_calls": snap["gc_collect_calls_so_far"]})
    checks.check(f"{tag}: {when}, no file under the runtime home is open",
                 snap["open_files"] == [], observed=snap["open_files"])
    checks.check(f"{tag}: {when}, no runtime-manager or pool-health-emitter thread is alive",
                 snap["start_threads_alive"] == [], observed=snap["start_threads_alive"])
    if backend == "postgres":
        checks.check(f"{tag}: {when}, 0 server connections to the case database (pg_stat_activity)",
                     snap.get("pg_connections", {}).get("count") == 0, observed=snap.get("pg_connections"))
    if os.name == "nt":
        rr = r.get("rename_and_remove") or {}
        checks.check(f"{tag}: {when}, on native Windows every file under the runtime home can be renamed and the "
                     f"home removed at once",
                     rr.get("not_renamable") == [] and rr.get("removed") is True,
                     observed=rr)
    if case == "control" and base is not None:
        b = (base.get("cases") or {}).get(f"{backend}-{case}") or {}
        checks.check(f"{tag}: the same owners are closed as on the 0.1.15 control", b.get("closes") == r.get("closes"),
                     observed={"0.1.16": r.get("closes"), "0.1.15": b.get("closes")})


def evaluate_engine(checks: Any, case: str, backend: str, r: dict[str, Any], base: dict[str, Any] | None) -> None:
    tag = f"item 3: {case} ({backend})"
    if "probe_error" in r:
        checks.check(f"{tag}: the case ran", False, observed={k: r.get(k) for k in ("probe_error", "child_stderr_tail")})
        return
    gc_ok = r.get("gc_enabled") is False and r.get("gc_collect_calls_so_far") == []
    if case != "engine-normal-close":
        checks.check(f"{tag}: the caller receives the original failure (the injected instance)",
                     r.get("original_exception") is True, observed=r.get("start"))
    if case in ("engine-blob-fails", "engine-vector-close-fails", "engine-record-close-fails", "engine-aliased"):
        expected = {"engine-blob-fails": ["record"], "engine-vector-close-fails": ["vector", "record"],
                    "engine-record-close-fails": ["record"], "engine-aliased": ["record"]}[case]
        meaning = {
            "engine-blob-fails": "the record store it created is closed, once; no vector store exists yet",
            "engine-vector-close-fails": "the vector store's close raises and the record store is still closed, "
                                         "each once, in StorageEngine.close() order",
            "engine-record-close-fails": "the record store's close raises; the original failure is kept",
            "engine-aliased": "the object that is both record and vector store is closed once",
        }[case]
        state = r.get("record_state") or {}
        released = (state.get("pg_connections", {}).get("count") == 0) if backend == "postgres" else (
            state.get("sqlite_connections_open") == [False])
        checks.check(f"{tag}: {meaning}", r.get("closes") == expected and gc_ok,
                     observed={"closes": r.get("closes"), "gc_collect_calls": r.get("gc_collect_calls_so_far")},
                     expected=expected)
        checks.check(f"{tag}: right after the exception the record store's "
                     + ("server connections are closed (0 in pg_stat_activity)" if backend == "postgres"
                        else "SQLite connection is closed"),
                     released, observed={**state, "pg_connections_at_failure_point": r.get("pg_connections_at_failure_point")})
    elif case.startswith("storage-borrowed-telemetry"):
        checks.check(f"{tag}: the engine build_runtime_storage owned is closed once"
                     + (" (its close raised after closing; the original failure is kept)" if case.endswith("close-fails") else ""),
                     r.get("engines_built") == 1 and r.get("closes") == ["storage_engine"] and gc_ok,
                     observed={"engines_built": r.get("engines_built"), "closes": r.get("closes")})
        if backend == "postgres":
            checks.check(f"{tag}: right after the exception, 0 server connections and no pool-health-emitter thread "
                         f"(the emitter was running while the engine was open)",
                         r.get("pg_connections", {}).get("count") == 0 and r.get("start_threads_alive") == []
                         and r.get("pool_health_emitter_started") is True,
                         observed={"pg_connections": r.get("pg_connections"), "threads": r.get("start_threads_alive"),
                                   "emitter_started": r.get("pool_health_emitter_started"),
                                   "pg_connections_with_engine_open": r.get("pg_connections_with_engine_open")})
        else:
            checks.check(f"{tag}: right after the exception the record store's SQLite connection is closed",
                         r.get("record_sqlite_connection_open") is False,
                         observed=r.get("record_sqlite_connection_open"))
        checks.check(f"{tag}: the borrowed TelemetryService is not closed by the storage; its owner closes it once "
                     f"afterwards",
                     r.get("telemetry_close_sync_calls_by_storage") == 0
                     and r.get("telemetry_close_sync_calls_after_caller_close") == 1,
                     observed={k: r.get(k) for k in ("telemetry_close_sync_calls_by_storage",
                                                    "telemetry_close_sync_calls_after_caller_close",
                                                    "open_files_before_caller_close",
                                                    "open_files_after_caller_close")})
    elif case == "engine-normal-close":
        obs = r.get("observed") or {}
        sep = obs.get("separate_stores") or {}
        ok = sep.get("closes") == ["vector", "record"] and not sep.get("emitter_running_after_close") \
            and sep.get("start_threads_alive") == []
        if backend == "postgres":
            ok = ok and sep.get("emitter_running_before_close") is True and sep.get("pg_connections", {}).get("count") == 0
        else:
            ok = ok and sep.get("sqlite_connections_open") == [False]
            ok = ok and (obs.get("aliased_store") or {}).get("closes") == ["record"] \
                and (obs.get("aliased_store") or {}).get("connection_open") is False
        vr = obs.get("vector_close_raises") or {}
        ok = ok and vr.get("closes") == ["vector"] and str(vr.get("close()", "")).startswith("OSError")
        checks.check(f"{tag}: StorageEngine.close() stops the emitter, closes the vector then the record store, each "
                     f"once (an aliased store once); a backend close that raises still propagates from close()",
                     ok, observed=obs)
        if base is not None:
            b = ((base.get("cases") or {}).get(f"{backend}-{case}") or {}).get("observed")
            checks.check(f"{tag}: the successful StorageEngine.close() behaves exactly as on the 0.1.15 control "
                         f"(every observation equal; the wait times of the connection counts are not compared)",
                         _without_waits(b) == _without_waits(obs), observed={"0.1.16": obs, "0.1.15": b})


def _without_waits(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_waits(v) for k, v in value.items() if k != "waited_s"}
    if isinstance(value, list):
        return [_without_waits(v) for v in value]
    return value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work", nargs="?")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--backend", default="all", choices=("all", "sqlite", "postgres"))
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--cases", default="")
    ap.add_argument("--child", default=None)
    ap.add_argument("--root", default=None)
    ap.add_argument("--child-out", default=None)
    ap.add_argument("--label", default="x")
    a = ap.parse_args()
    if a.child:
        try:
            if a.child in RUNTIME_CASES:
                result = child_runtime(a.child, a.backend, Path(a.root).resolve(), a.label)
            else:
                result = child_engine(a.child, a.backend, Path(a.root).resolve(), a.label)
        except BaseException as exc:  # noqa: BLE001
            result = {"case": a.child, "probe_error": describe(exc), "traceback": traceback.format_exc()}
        Path(a.child_out).write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        return 0

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _common import Checks, banner, quiet, write_result  # noqa: E402

    quiet()
    import openminion

    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    backends = ["sqlite", "postgres"] if a.backend == "all" else [a.backend]
    if "postgres" in backends and not PGURL:
        backends.remove("postgres")
        print("NOT RUN postgres: no PGURL")
    info = banner("probe_447", backends=backends, pgurl_set=bool(PGURL))
    if PGURL:
        try:
            import sqlalchemy as sa

            engine = _admin_engine()
            with engine.connect() as conn:
                info["postgres"] = conn.execute(sa.text("select version()")).scalar_one()
            engine.dispose()
            import psycopg

            info["sqlalchemy"], info["psycopg"] = sa.__version__, psycopg.__version__
        except Exception as exc:  # noqa: BLE001
            info["postgres"] = f"unavailable: {describe(exc)}"
    import psutil

    info["psutil"] = psutil.__version__
    base = json.loads(Path(a.baseline).read_text(encoding="utf-8")) if a.baseline else None
    label = "v" + str(getattr(openminion, "__version__", "x")).replace(".", "")
    wanted = [c for c in a.cases.split(",") if c]
    checks = Checks()
    checks.check("item 5: the installed package under test is imported from site-packages, outside any checkout, "
                 "with python -I",
                 info.get("imported_from_site_packages") is True and info.get("isolated_mode") is True,
                 observed={k: info.get(k) for k in ("openminion.__version__", "openminion.__file__", "python",
                                                    "platform", "sqlite3.sqlite_version", "postgres")})
    results: dict[str, Any] = {}
    for backend in backends:
        for case, spec in list(RUNTIME_CASES.items()) + list(ENGINE_CASES.items()):
            if wanted and case not in wanted:
                continue
            if backend not in spec[-1]:
                continue
            started = time.monotonic()
            r = run_child(case, backend, work, label)
            r["seconds"] = round(time.monotonic() - started, 1)
            results[f"{backend}-{case}"] = r
            print(f"=== {backend}-{case} ({r['seconds']} s) start={str(r.get('start'))[:90]!r} "
                  f"closes={r.get('closes')}", flush=True)
            if case in RUNTIME_CASES:
                evaluate_runtime(checks, case, backend, r, base)
            else:
                evaluate_engine(checks, case, backend, r, base)
    summary = checks.summary()
    write_result(Path(a.out), {"banner": info, "baseline": a.baseline, "summary": summary,
                               "checks": checks.items, "cases": results})
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
