"""External memory provider: what does SDK shutdown close? (#388)

An SDK runtime (echo provider, explicit roots) is built and closed twice:
- the default memory provider (control);
- the `external` provider with the bundled `reference-sqlite` adapter, whose
  backend (SophiaGraphSqliteStore) has no close();
- the `external` provider with the same engine plus a counting close(),
  registered as a second adapter;
- the `external` provider with a `memory.store` setting that cannot be used
  (`backend: postgres` without a URL).
No turn runs: an echo turn with an external adapter fails in
apply_capture_bundle(), a separate question, and the close chain does not
depend on a turn.

For each run it reports the stores `resolve_memory_backend` created and their
close counts, the audit sinks the assembly created, every
RuntimeMemoryAssembly.close() outcome (APIRuntime.close() suppresses
component close errors, so the probe records them), the close counts of the
artifact store the assembly obtained and of an external backend with close(),
and whether that artifact store still accepts a write after shutdown.

With OPENMINION_TEST_POSTGRES_URL set it adds an `external` run whose
`memory.store` is PostgreSQL and reports that store's engine disposals, the
server connections with the run's application_name after the build and after
shutdown, and the tables created in the run's schema.

Run from an openminion source checkout: PYTHONPATH="src;." python probe_388.py
(PYTHONPATH=src:. on Linux/macOS)
"""

import json
import logging
import os
import sys
import tempfile
import uuid
from pathlib import Path

from sophiagraph.storage import SophiaGraphSqliteStore

from openminion import APIRuntime
from openminion.base.config import ConfigManager, OpenMinionConfig, save_config
from openminion.modules.artifact.control import ArtifactCtl
from openminion.modules.memory.backends.external import (
    register_external_backend,
    register_reference_sqlite_backend,
)
from openminion.modules.memory.runtime.assembly import RuntimeMemoryAssembly
from openminion.modules.memory.storage.audit import SQLiteMemoryAuditSink
from openminion.services.runtime import memory as runtime_memory
from tests._csc_fixtures import _csc_install_default_agent

BACKEND_CLOSES: list[int] = []


class ClosingReferenceBackend(SophiaGraphSqliteStore):
    def close(self) -> None:
        BACKEND_CLOSES.append(id(self))


def build_closing_backend(*, config, **_):
    return ClosingReferenceBackend(Path(str(config.options["db_path"])))


def run_case(root, memory_config, pg=None):
    config = OpenMinionConfig()
    _csc_install_default_agent(config, provider="echo")
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / ".openminion" / "state.db")
    config_path = root / "agents.json"
    save_config(config, str(config_path))
    manager = ConfigManager.load(
        str(config_path), home_root=root, data_root=root / ".openminion"
    )
    if memory_config is not None:
        manager.register("memory", lambda **kwargs: memory_config)

    stores, store_closes, sinks, artifact_closes, outcomes = [], [], [], [], []
    BACKEND_CLOSES.clear()
    resolve_backend = runtime_memory.resolve_memory_backend
    create_sink = runtime_memory.SQLiteMemoryAuditSink
    store_close = {}

    def record_backend(**kwargs):
        resolved = resolve_backend(**kwargs)
        stores.append(resolved.store)
        cls = type(resolved.store)
        if cls not in store_close:
            store_close[cls] = cls.close

            def counting_close(self, _orig=cls.close):
                store_closes.append(id(self))
                return _orig(self)

            cls.close = counting_close
        if pg is not None:
            from sqlalchemy import event

            event.listen(
                resolved.store._engine,
                "engine_disposed",
                lambda engine: pg["disposals"].append(1),
            )
        return resolved

    def record_sink(db_path):
        sink = create_sink(db_path)
        sinks.append(sink)
        return sink

    assembly_close = RuntimeMemoryAssembly.close

    def recording_close(self):
        try:
            result = assembly_close(self)
            outcomes.append(result.reason_code)
            return result
        except Exception as exc:
            outcomes.append(f"raised {type(exc).__name__}: {exc}")
            raise

    artifact_close = ArtifactCtl.close

    def counting_artifact_close(self):
        artifact_closes.append(id(self))
        return artifact_close(self)

    runtime_memory.resolve_memory_backend = record_backend
    runtime_memory.SQLiteMemoryAuditSink = record_sink
    RuntimeMemoryAssembly.close = recording_close
    ArtifactCtl.close = counting_artifact_close
    runtime = None
    try:
        try:
            runtime = APIRuntime.from_manager(manager)
        except Exception as exc:
            return {
                "build": f"{type(exc).__name__}: {exc}",
                "stores_created_by_resolve_memory_backend": [
                    type(store).__name__ for store in stores
                ],
            }
        [assembly] = list(runtime._memory_assemblies.values())
        row = {
            "build": "ok",
            "stores_created_by_resolve_memory_backend": [
                type(store).__name__ for store in stores
            ],
            "audit_sinks_created": len(sinks),
            "store_closed_by_memory_service": type(assembly.service._store).__name__,
        }
        if pg is not None:
            row["server_connections_after_build"] = pg["count"]()
        per_close = []
        for _ in range(2):
            runtime.close()
            per_close.append(
                {
                    "assembly_close_calls": len(outcomes),
                    "created_store_close_calls": [
                        store_closes.count(id(store)) for store in stores
                    ],
                    "external_backend_close_calls": len(BACKEND_CLOSES),
                    "assembly_artifact_store_close_calls": artifact_closes.count(
                        id(assembly.artifactctl)
                    ),
                }
            )
        try:
            assembly.artifactctl.ingest_bytes(b"after shutdown")
            write_after = "accepted"
        except Exception as exc:
            write_after = f"{type(exc).__name__}: {exc}"
        row["after_each_runtime_close"] = per_close
        row["assembly_close_outcomes"] = outcomes
        row["assembly_artifact_store_write_after_shutdown"] = write_after
        if pg is not None:
            row["engine_dispose_calls"] = len(pg["disposals"])
            row["server_connections_after_close"] = pg["settled"]()
            row["tables_in_run_schema"] = pg["tables"]()
        return row
    finally:
        runtime_memory.resolve_memory_backend = resolve_backend
        runtime_memory.SQLiteMemoryAuditSink = create_sink
        RuntimeMemoryAssembly.close = assembly_close
        ArtifactCtl.close = artifact_close
        for cls, orig in store_close.items():
            cls.close = orig
        if runtime is not None:
            for assembly in runtime._memory_assemblies.values():
                if assembly.artifactctl is not None:
                    artifact_close(assembly.artifactctl)
        for store in stores:
            if pg is not None:
                store._engine.dispose()
        for sink in sinks:
            sink.close()


def postgres_case(base, root, external):
    import time
    from urllib.parse import quote_plus

    import sqlalchemy as sa

    token = uuid.uuid4().hex[:12]
    schema, app = f"probe_388_{token}", f"probe_388_{token}"
    url = (
        f"{base}?options={quote_plus(f'-csearch_path={schema}')}"
        f"&application_name={app}"
    )
    admin = sa.create_engine(base, future=True)
    with admin.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    def count():
        with admin.connect() as conn:
            return conn.execute(
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name = :n"
                ),
                {"n": app},
            ).scalar_one()

    def settled():
        deadline = time.monotonic() + 5.0
        value = count()
        while value and time.monotonic() < deadline:
            time.sleep(0.05)
            value = count()
        return value

    def tables():
        with admin.connect() as conn:
            return conn.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = :s"
                ),
                {"s": schema},
            ).scalar_one()

    try:
        memory_config = {
            "store": {"backend": "postgres", "postgres": {"url": url}},
            **external,
        }
        pg = {"disposals": [], "count": count, "settled": settled, "tables": tables}
        return run_case(root, memory_config, pg=pg)
    finally:
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def main():
    logging.disable(logging.WARNING)
    register_reference_sqlite_backend()
    register_external_backend(
        "closing-reference-sqlite", factory=build_closing_backend
    )
    rows = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        os.environ["OPENMINION_HOME"] = str(tmp / "home")
        os.environ["OPENMINION_DATA_ROOT"] = str(tmp / "home" / "data")

        def external(name, adapter="reference-sqlite"):
            return {
                "backend": {
                    "provider": "external",
                    "external_adapter": adapter,
                    "options": {"db_path": str(tmp / f"{name}-external.sqlite3")},
                }
            }

        def sqlite_store(name):
            return {"backend": "sqlite", "sqlite_path": str(tmp / f"{name}-memory.db")}

        rows["default provider (control)"] = run_case(tmp / "a", None)
        rows["external reference-sqlite (no close), SQLite memory.store"] = run_case(
            tmp / "b", {"store": sqlite_store("b"), **external("b")}
        )
        rows["external backend with close(), SQLite memory.store"] = run_case(
            tmp / "c",
            {
                "store": sqlite_store("c"),
                **external("c", adapter="closing-reference-sqlite"),
            },
        )
        rows["external reference-sqlite, memory.store postgres without URL"] = (
            run_case(tmp / "d", {"store": {"backend": "postgres"}, **external("d")})
        )
        base = os.environ.get("OPENMINION_TEST_POSTGRES_URL")
        if base:
            rows["external reference-sqlite, PostgreSQL memory.store"] = (
                postgres_case(base, tmp / "e", external("e"))
            )
    print(json.dumps(rows, indent=2))
    print(sys.version.split()[0], sys.platform)


if __name__ == "__main__":
    main()
