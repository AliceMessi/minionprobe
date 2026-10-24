"""Bootstrap gateway fallback: who can close what its memory assembly creates?

`build_agent_memory_service` (the fallback `build_gateway_service` uses when no
`agent_memory` is passed) builds a full runtime memory assembly through
`_build_memory_v2_gateway_adapter` and returns only the assembly's gateway.
The probe builds it, records one turn through the returned adapter (so the
audit sink opens its connection), drops its own reference to the assembly
and reports what was closed. The control builds the same configuration
through `build_runtime_memory_assembly`, the builder the SDK runtime retains,
and closes that assembly once.

For the fallback it also reports whether the module-level backend factory
registry still references the store after the call. With OPENMINION_TEST_POSTGRES_URL set it
repeats the fallback with a PostgreSQL memory store and reports the engine's
server connections.

Run from an openminion source checkout: PYTHONPATH="src;." python probe_fallback_retention.py
(PYTHONPATH=src:. on Linux/macOS)
"""

import gc
import json
import logging
import os
import sys
import tempfile
import uuid
import weakref
from pathlib import Path

from openminion.base.config import ConfigManager, OpenMinionConfig, save_config
from openminion.modules.artifact.control import ArtifactCtl
from openminion.modules.memory.backends import get_registered_backend_factory
from openminion.modules.memory.runtime.assembly import RuntimeMemoryAssembly
from openminion.modules.memory.storage.audit import SQLiteMemoryAuditSink
from openminion.services.runtime import memory as runtime_memory
from openminion.services.runtime.bootstrap import build_agent_memory_service
from tests._csc_fixtures import _csc_install_default_agent


def registry_holds(store):
    """True when the module-level `sophiagraph` backend factory closes over this store."""
    factory = get_registered_backend_factory("sophiagraph")
    cells = [cell.cell_contents for cell in factory.__closure__ or ()]
    return any(getattr(cell, "_store", None) is store for cell in cells)


def run_case(root, memory_config, *, fallback, pg=None):
    config = OpenMinionConfig()
    _csc_install_default_agent(config, provider="echo")
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"
    config_path = root / "agents.json"
    save_config(config, str(config_path))
    manager = ConfigManager.load(str(config_path), home_root=root, data_root=root / "data")
    manager.register("memory", lambda **kwargs: memory_config)

    created = {"store": [], "sink": [], "artifact": [], "assembly": []}
    closes = []
    patches = []

    def patch(cls, name, wrapper):
        original = getattr(cls, name)
        patches.append((cls, name, original))
        setattr(cls, name, wrapper(original))

    def record_init(kind):
        def wrapper(original):
            def init(self, *args, **kwargs):
                original(self, *args, **kwargs)
                created[kind].append(self)
            return init
        return wrapper

    def count_close(original):
        def close(self, *args, **kwargs):
            closes.append(id(self))
            return original(self, *args, **kwargs)
        return close

    resolve_backend = runtime_memory.resolve_memory_backend

    def record_backend(**kwargs):
        resolved = resolve_backend(**kwargs)
        created["store"].append(resolved.store)
        cls = type(resolved.store)
        if not any(p[0] is cls and p[1] == "close" for p in patches):
            patch(cls, "close", count_close)
        return resolved

    patch(SQLiteMemoryAuditSink, "__init__", record_init("sink"))
    patch(SQLiteMemoryAuditSink, "close", count_close)
    patch(ArtifactCtl, "__init__", record_init("artifact"))
    patch(ArtifactCtl, "close", count_close)
    patch(RuntimeMemoryAssembly, "__init__", record_init("assembly"))
    patch(RuntimeMemoryAssembly, "close", count_close)
    runtime_memory.resolve_memory_backend = record_backend
    try:
        kwargs = dict(
            config=config,
            agent_id="alpha",
            memory_root=root / "memory",
            logger=logging.getLogger("probe"),
            config_manager=manager,
            home_root=root,
            data_root=root / "data",
        )
        if fallback:
            gateway = build_agent_memory_service(**kwargs)
        else:
            assembly = runtime_memory.build_runtime_memory_assembly(**kwargs)
            gateway = assembly.gateway
        gateway.record_turn(
            session_id="s1", run_id="r1", request_id="q1", channel="console",
            target="probe", user_message="Remember: the probe colour is green.",
            assistant_message="Noted.",
        )
        [store], [sink] = created["store"], created["sink"]
        sink_connected = sink._conn is not None
        [assembly_obj] = created["assembly"]
        [artifact] = [a for a in created["artifact"] if a is assembly_obj.artifactctl]
        assembly_ref = weakref.ref(assembly_obj)
        created["assembly"].clear()
        del assembly_obj
        row = {"builder": "build_agent_memory_service" if fallback else "build_runtime_memory_assembly"}
        returned = gateway if fallback else assembly
        row["returned"] = type(returned).__name__
        row["returned_object_has_close"] = callable(getattr(returned, "close", None))
        del returned
        if fallback:
            gc.collect()
            row["assembly_alive_after_return"] = assembly_ref() is not None
        else:
            assembly.close()
            row["assembly_close_calls"] = closes.count(id(assembly))
        row["audit_sink_connected_during_turn"] = sink_connected
        row["close_calls"] = {
            f"memory store ({type(store).__name__})": closes.count(id(store)),
            "audit sink": closes.count(id(sink)),
            "assembly artifact store": closes.count(id(artifact)),
        }
        row["audit_sink_connection_open"] = sink._conn is not None
        try:
            artifact.ingest_bytes(b"after")
            row["assembly_artifact_store_accepts_write"] = True
        except Exception:
            row["assembly_artifact_store_accepts_write"] = False
        if fallback:
            row["module_backend_factory_registry_references_this_store"] = registry_holds(store)
        if pg is not None:
            row["server_connections_while_gateway_alive"] = pg["count"]()
            row["engine_dispose_calls"] = len(pg["disposals"])
            store._engine.dispose()
        # cleanup (not runtime behaviour)
        for original_close, obj in ((SQLiteMemoryAuditSink, sink), (ArtifactCtl, artifact)):
            if id(obj) not in closes:
                [orig] = [p[2] for p in patches if p[0] is original_close and p[1] == "close"]
                orig(obj)
        return row
    finally:
        runtime_memory.resolve_memory_backend = resolve_backend
        for cls, name, original in reversed(patches):
            setattr(cls, name, original)


def postgres_case(base, root):
    import sqlalchemy as sa
    from sqlalchemy import event
    from urllib.parse import quote_plus

    token = uuid.uuid4().hex[:12]
    schema, app = f"probe_fb_{token}", f"probe_fb_{token}"
    url = f"{base}?options={quote_plus(f'-csearch_path={schema}')}&application_name={app}"
    admin = sa.create_engine(base, future=True)
    with admin.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    disposals = []

    def count():
        with admin.connect() as conn:
            return conn.execute(
                sa.text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :n"),
                {"n": app},
            ).scalar_one()

    resolve_backend = runtime_memory.resolve_memory_backend

    def listen_backend(**kwargs):
        resolved = resolve_backend(**kwargs)
        event.listen(resolved.store._engine, "engine_disposed", lambda e: disposals.append(1))
        return resolved

    runtime_memory.resolve_memory_backend = listen_backend
    try:
        memory_config = {"store": {"backend": "postgres", "postgres": {"url": url}}}
        return run_case(root, memory_config, fallback=True, pg={"count": count, "disposals": disposals})
    finally:
        runtime_memory.resolve_memory_backend = resolve_backend
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def main():
    logging.disable(logging.WARNING)
    rows = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        os.environ["OPENMINION_HOME"] = str(tmp / "home")
        os.environ["OPENMINION_DATA_ROOT"] = str(tmp / "home" / "data")
        for name, fallback in (("control: runtime assembly builder", False), ("bootstrap fallback", True)):
            root = tmp / name.split(":")[0].replace(" ", "-")
            root.mkdir()
            sqlite = {"store": {"backend": "sqlite", "sqlite_path": str(root / "memory.db")}}
            rows[f"{name}, SQLite memory store"] = run_case(root, sqlite, fallback=fallback)
        base = os.environ.get("OPENMINION_TEST_POSTGRES_URL")
        if base:
            root = tmp / "pg"
            root.mkdir()
            rows["bootstrap fallback, PostgreSQL memory store"] = postgres_case(base, root)
    print(json.dumps(rows, indent=2))
    print(sys.version.split()[0], sys.platform)


if __name__ == "__main__":
    main()
