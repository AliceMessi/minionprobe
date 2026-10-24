"""Runtime memory assembly: what stays open when building fails partway?

`build_runtime_memory_assembly` (the builder APIRuntime uses) is called with a
memory config whose backend provider is `external` with an adapter name that
is not registered, as in the existing
test_build_agent_memory_service_rejects_unimplemented_external_backend. The
build raises in `instantiate_backend`, after the assembly has already obtained
its artifact store, created the memory store and created the audit sink. The
probe reports what was closed when the exception reached the caller, and
whether the module-level backend factory registry still references the store.

With OPENMINION_TEST_POSTGRES_URL set it repeats the build with a PostgreSQL
memory store and reports the engine's server connections.

Run from an openminion source checkout: PYTHONPATH="src;." python probe_construction_failure.py
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
from openminion.modules.memory.storage.audit import SQLiteMemoryAuditSink
from openminion.services.runtime import memory as runtime_memory
from tests._csc_fixtures import _csc_install_default_agent


def registry_holds(store):
    """True when the module-level `sophiagraph` backend factory closes over this store."""
    factory = get_registered_backend_factory("sophiagraph")
    cells = [cell.cell_contents for cell in factory.__closure__ or ()]
    return any(getattr(cell, "_store", None) is store for cell in cells)


def run_case(root, store_config, pg=None):
    config = OpenMinionConfig()
    _csc_install_default_agent(config, provider="echo")
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"
    config_path = root / "agents.json"
    save_config(config, str(config_path))
    manager = ConfigManager.load(str(config_path), home_root=root, data_root=root / "data")
    memory_config = {
        "store": store_config,
        "backend": {"provider": "external", "external_adapter": "not_registered"},
    }
    manager.register("memory", lambda **kwargs: memory_config)

    created = {"store": [], "sink": [], "artifact": []}
    closes = []
    originals = {
        "sink_init": SQLiteMemoryAuditSink.__init__,
        "sink_close": SQLiteMemoryAuditSink.close,
        "artifact_init": ArtifactCtl.__init__,
        "artifact_close": ArtifactCtl.close,
    }
    store_close = {}

    def recorder(kind, original):
        def init(self, *args, **kwargs):
            original(self, *args, **kwargs)
            created[kind].append(self)
        return init

    def counter(original):
        def close(self, *args, **kwargs):
            closes.append(id(self))
            return original(self, *args, **kwargs)
        return close

    resolve_backend = runtime_memory.resolve_memory_backend

    def record_backend(**kwargs):
        resolved = resolve_backend(**kwargs)
        created["store"].append(resolved.store)
        cls = type(resolved.store)
        if cls not in store_close:
            store_close[cls] = cls.close
            cls.close = counter(cls.close)
        if pg is not None:
            from sqlalchemy import event

            event.listen(resolved.store._engine, "engine_disposed", lambda e: pg["disposals"].append(1))
        return resolved

    SQLiteMemoryAuditSink.__init__ = recorder("sink", originals["sink_init"])
    SQLiteMemoryAuditSink.close = counter(originals["sink_close"])
    ArtifactCtl.__init__ = recorder("artifact", originals["artifact_init"])
    ArtifactCtl.close = counter(originals["artifact_close"])
    runtime_memory.resolve_memory_backend = record_backend
    try:
        try:
            runtime_memory.build_runtime_memory_assembly(
                config=config,
                agent_id="alpha",
                memory_root=root / "memory",
                logger=logging.getLogger("probe"),
                config_manager=manager,
                home_root=root,
                data_root=root / "data",
            )
            outcome = "built"
        except Exception as exc:
            outcome = f"{type(exc).__name__}: {exc}"
        [store], [sink], [artifact] = created["store"], created["sink"], created["artifact"]
        row = {
            "build": outcome,
            "created_before_the_failure": {
                "assembly artifact store": type(artifact).__name__,
                "memory store": type(store).__name__,
                "audit sink": type(sink).__name__,
            },
            "close_calls_after_the_failure": {
                "assembly artifact store": closes.count(id(artifact)),
                "memory store": closes.count(id(store)),
                "audit sink": closes.count(id(sink)),
            },
            "audit_sink_connection_open": sink._conn is not None,
        }
        row["module_backend_factory_registry_references_this_store"] = registry_holds(store)
        try:
            artifact.ingest_bytes(b"after failure")
            row["assembly_artifact_store_accepts_write"] = True
        except Exception:
            row["assembly_artifact_store_accepts_write"] = False
        originals["artifact_close"](artifact)  # cleanup, not runtime behaviour
        if pg is not None:
            row["engine_dispose_calls"] = len(pg["disposals"])
            row["server_connections_after_the_failure"] = pg["count"]()
            engine = weakref.ref(store._engine)
            created.clear()
            del store, sink, artifact
            gc.collect()
            row["server_connections_after_dropping_references_and_gc"] = pg["count"]()
            if engine() is not None:
                engine().dispose()  # cleanup, not runtime behaviour
        return row
    finally:
        runtime_memory.resolve_memory_backend = resolve_backend
        SQLiteMemoryAuditSink.__init__ = originals["sink_init"]
        SQLiteMemoryAuditSink.close = originals["sink_close"]
        ArtifactCtl.__init__ = originals["artifact_init"]
        ArtifactCtl.close = originals["artifact_close"]
        for cls, original in store_close.items():
            cls.close = original


def postgres_case(base, root):
    import sqlalchemy as sa
    from urllib.parse import quote_plus

    token = uuid.uuid4().hex[:12]
    schema, app = f"probe_cf_{token}", f"probe_cf_{token}"
    url = f"{base}?options={quote_plus(f'-csearch_path={schema}')}&application_name={app}"
    admin = sa.create_engine(base, future=True)
    with admin.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    def count():
        with admin.connect() as conn:
            return conn.execute(
                sa.text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :n"),
                {"n": app},
            ).scalar_one()

    def tables():
        with admin.connect() as conn:
            return conn.execute(
                sa.text("SELECT count(*) FROM information_schema.tables WHERE table_schema = :s"),
                {"s": schema},
            ).scalar_one()

    try:
        row = run_case(root, {"backend": "postgres", "postgres": {"url": url}},
                       pg={"count": count, "disposals": []})
        row["tables_bootstrapped_in_probe_schema"] = tables()
        return row
    finally:
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
        root = tmp / "sqlite"
        root.mkdir()
        rows["SQLite memory store"] = run_case(
            root, {"backend": "sqlite", "sqlite_path": str(root / "memory.db")}
        )
        base = os.environ.get("OPENMINION_TEST_POSTGRES_URL")
        if base:
            root = tmp / "pg"
            root.mkdir()
            rows["PostgreSQL memory store"] = postgres_case(base, root)
    gc.collect()
    print(json.dumps(rows, indent=2))
    print(sys.version.split()[0], sys.platform)


if __name__ == "__main__":
    main()
