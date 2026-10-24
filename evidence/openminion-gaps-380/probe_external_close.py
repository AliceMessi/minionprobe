"""External memory provider: what does SDK shutdown close?

An SDK runtime (echo provider, explicit roots) is built and closed twice, once
with the default memory provider and once with the `external` provider (the
bundled `reference-sqlite` adapter). No turn runs: on this tree an echo turn
with `reference-sqlite` fails in apply_capture_bundle(), which is a separate
question, and the close chain does not depend on a turn.

For each run it reports the store the memory service closes, every
RuntimeMemoryAssembly.close() call and its outcome (APIRuntime.close()
suppresses component close errors, so the probe records them), and the close
counts of the store `resolve_memory_backend` created and of the artifact store
the assembly obtained.

With OPENMINION_TEST_POSTGRES_URL set it adds an `external` run whose memory
store is PostgreSQL and reports that engine's disposals and server connections.

Run from an openminion source checkout: PYTHONPATH="src;." python probe_external_close.py
(PYTHONPATH=src:. on Linux/macOS)
"""

import json
import logging
import os
import sys
import tempfile
import uuid
from pathlib import Path

from openminion import APIRuntime
from openminion.base.config import ConfigManager, OpenMinionConfig, save_config
from openminion.modules.artifact.control import ArtifactCtl
from openminion.modules.memory.backends.external import (
    register_reference_sqlite_backend,
)
from openminion.modules.memory.runtime.assembly import RuntimeMemoryAssembly
from openminion.services.runtime import memory as runtime_memory
from tests._csc_fixtures import _csc_install_default_agent


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

    stores, store_closes, artifact_closes, outcomes = [], [], [], []
    resolve_backend = runtime_memory.resolve_memory_backend
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
    RuntimeMemoryAssembly.close = recording_close
    ArtifactCtl.close = counting_artifact_close
    try:
        runtime = APIRuntime.from_manager(manager)
        [assembly] = list(runtime._memory_assemblies.values())
        [store] = stores
        conns_before_close = pg["count"]() if pg else None
        per_close = []
        for _ in range(2):
            runtime.close()
            per_close.append(
                {
                    "assembly_close_calls": len(outcomes),
                    "created_store_close_calls": store_closes.count(id(store)),
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
        row = {
            "store_created_by_resolve_memory_backend": type(store).__name__,
            "store_closed_by_memory_service": type(assembly.service._store).__name__,
            "after_each_runtime_close": per_close,
            "assembly_close_outcomes": outcomes,
            "assembly_artifact_store_write_after_shutdown": write_after,
        }
        if pg is not None:
            row["engine_dispose_calls"] = len(pg["disposals"])
            row["server_connections_before_close"] = conns_before_close
            row["server_connections_after_close"] = pg["count"]()
            store._engine.dispose()
        if assembly.artifactctl is not None:
            artifact_close(assembly.artifactctl)
        return row
    finally:
        runtime_memory.resolve_memory_backend = resolve_backend
        RuntimeMemoryAssembly.close = assembly_close
        ArtifactCtl.close = artifact_close
        for cls, orig in store_close.items():
            cls.close = orig


def postgres_case(base, root, external):
    import sqlalchemy as sa
    from urllib.parse import quote_plus

    token = uuid.uuid4().hex[:12]
    schema, app = f"probe_ext_{token}", f"probe_ext_{token}"
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

    try:
        memory_config = {"store": {"backend": "postgres", "postgres": {"url": url}}, **external}
        return run_case(root, memory_config, pg={"disposals": [], "count": count})
    finally:
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def main():
    logging.disable(logging.WARNING)
    register_reference_sqlite_backend()
    rows = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        os.environ["OPENMINION_HOME"] = str(tmp / "home")
        os.environ["OPENMINION_DATA_ROOT"] = str(tmp / "home" / "data")

        def external(name):
            return {
                "backend": {
                    "provider": "external",
                    "external_adapter": "reference-sqlite",
                    "options": {"db_path": str(tmp / f"{name}-external.sqlite3")},
                }
            }

        rows["default provider (control)"] = run_case(tmp / "a", None)
        rows["external provider, SQLite memory store"] = run_case(
            tmp / "b",
            {"store": {"backend": "sqlite", "sqlite_path": str(tmp / "b-memory.db")}, **external("b")},
        )
        base = os.environ.get("OPENMINION_TEST_POSTGRES_URL")
        if base:
            rows["external provider, PostgreSQL memory store"] = postgres_case(
                base, tmp / "c", external("c")
            )
    print(json.dumps(rows, indent=2))
    print(sys.version.split()[0], sys.platform)


if __name__ == "__main__":
    main()
