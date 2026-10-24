"""Issue #368 ownership audit: which store does the runtime memory assembly create
for each memory backend, which store does its memory service close, and does
closing the assembly twice reach the created store?

No live PostgreSQL: the postgres row injects an engine spy for
sqlalchemy.create_engine and skips the schema bootstrap, so it shows ownership
only. pg_close_chain_368.py checks a live server.

Run from an openminion checkout: PYTHONPATH=src python probe_368.py
(on Windows: set PYTHONPATH=src)
"""

import json
import logging
import tempfile
from pathlib import Path

import sqlalchemy

from openminion.base.config import OpenMinionConfig
from openminion.modules.memory.backends.external import (
    register_reference_sqlite_backend,
)
from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore
from openminion.services.runtime import memory as runtime_memory


class EngineSpy:
    def __init__(self):
        self.dispose_calls = 0

    def dispose(self):
        self.dispose_calls += 1


class ArtifactStoreSpy:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


def run_case(root, memory_config):
    created = []
    resolve_backend = runtime_memory.resolve_memory_backend

    def record_backend(**kwargs):
        resolved = resolve_backend(**kwargs)
        created.append(resolved.store)
        return resolved

    runtime_memory.resolve_memory_backend = record_backend
    artifact_store = ArtifactStoreSpy()
    config = OpenMinionConfig()
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"
    try:
        assembly = runtime_memory.build_memory_v2_runtime_assembly(
            config=config,
            agent_id="alpha",
            memory_root=root,
            logger=logging.getLogger("probe_368"),
            config_manager=None,
            home_root=None,
            data_root=None,
            session_context=None,
            retrieve_ctl=None,
            storage_path=None,
            resolve_runtime_memory_config_fn=lambda **kwargs: memory_config,
            artifactctl_factory=lambda: artifact_store,
        )
    finally:
        runtime_memory.resolve_memory_backend = resolve_backend
    [store] = created
    store_type = type(store)
    store_close = getattr(store_type, "close", None)
    store_closes = [0]
    if store_close is not None:

        def counting_close(self):
            if self is store:
                store_closes[0] += 1
            store_close(self)

        store_type.close = counting_close
    closes = []
    try:
        for _ in range(2):
            try:
                outcome = assembly.close().reason_code
            except Exception as exc:  # noqa: BLE001 - the probe reports it
                outcome = f"{type(exc).__name__}: {exc}"
            closes.append(
                {
                    "result": outcome,
                    "store_close_calls": store_closes[0],
                    "engine_dispose_calls": getattr(
                        getattr(store, "_engine", None), "dispose_calls", None
                    ),
                    "artifact_store_close_calls": artifact_store.close_calls,
                }
            )
    finally:
        if store_close is not None:
            store_type.close = store_close
    return {
        "created_store": store_type.__name__,
        "created_store_has_close": store_close is not None,
        "service_closes": type(assembly.service._store).__name__,
        "assembly_closes": closes,
    }


def main():
    register_reference_sqlite_backend()
    create_engine = sqlalchemy.create_engine
    bootstrap = PostgresMemoryStore._bootstrap_schema
    sqlalchemy.create_engine = lambda *args, **kwargs: EngineSpy()
    PostgresMemoryStore._bootstrap_schema = lambda self, placeholder_path: None
    rows = {}
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cases = {
                "sqlite (default runtime config)": None,
                "postgres (engine spy)": {
                    "store": {
                        "backend": "postgres",
                        "postgres": {"url": "postgresql+psycopg://probe/memory"},
                    }
                },
                "mock": {"store": {"backend": "mock"}},
                "remote": {
                    "store": {
                        "backend": "remote",
                        "remote": {"endpoint": "http://127.0.0.1:9"},
                    }
                },
                "sqlite, external provider (reference-sqlite)": {
                    "store": {"backend": "sqlite"},
                    "backend": {
                        "provider": "external",
                        "external_adapter": "reference-sqlite",
                        "options": {"db_path": str(Path(tmp) / "external.sqlite3")},
                    },
                },
            }
            for index, (name, memory_config) in enumerate(cases.items()):
                root = Path(tmp) / f"case-{index}"
                root.mkdir()
                if memory_config is None:
                    memory_config = runtime_memory.memory_runtime_configuration.resolve_runtime_memory_config(
                        config=OpenMinionConfig(), memory_root=root
                    )
                rows[name] = run_case(root, memory_config)
    finally:
        sqlalchemy.create_engine = create_engine
        PostgresMemoryStore._bootstrap_schema = bootstrap
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
