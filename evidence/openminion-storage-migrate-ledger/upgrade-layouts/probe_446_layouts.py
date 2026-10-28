"""#446 upgrade impact: what PostgreSQL module stores leave in a database today.

Each case uses a new, empty PostgreSQL database and the code of the checkout on PYTHONPATH:
- memory_dedicated: PostgresMemoryStore on its own URL, one record, closed, opened again;
- memory_after_runtime / memory_before_runtime: runtime storage and PostgresMemoryStore on one URL;
- a2a_audit_after_runtime / a2a_audit_alone: the A2A PostgresAuditStore on the runtime storage URL.
After each step it prints the schemas, tables (with row counts), alembic_version, om_meta,
the rows of `migrations`, and the columns of memory_records / idempotency_keys.
"""

import json
import os
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

import sqlalchemy as sa

BASE = os.environ["OPENMINION_TEST_POSTGRES_URL"]
ROOT = Path(tempfile.mkdtemp(prefix="l446-"))
os.environ["OPENMINION_HOME"] = str(ROOT / "home")


def new_db() -> str:
    name = f"l446_{uuid.uuid4().hex[:10]}"
    admin = sa.create_engine(BASE, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return BASE.rsplit("/", 1)[0] + "/" + name


def inventory(url: str) -> dict:
    engine = sa.create_engine(url)
    out: dict = {}
    with engine.connect() as conn:

        def q(sql: str):
            try:
                return [list(r) for r in conn.execute(sa.text(sql))]
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                return f"ERR {type(exc).__name__}"

        out["schemas"] = [r[0] for r in q(
            "select schema_name from information_schema.schemata "
            "where schema_name not like 'pg_%' and schema_name <> 'information_schema' order by 1")]
        tables = {}
        for schema, table in q(
            "select table_schema, table_name from information_schema.tables "
            "where table_schema not in ('pg_catalog','information_schema') and table_type='BASE TABLE' order by 1,2"
        ):
            tables[f"{schema}.{table}"] = q(f'select count(*) from "{schema}"."{table}"')[0][0]
        out["tables"] = tables
        out["alembic_version"] = q("select version_num from alembic_version")
        out["om_meta"] = q("select key, value from om_meta where key in ('module_id','schema_head') order by key")
        out["migrations_rows"] = q("select version, name from migrations order by version")
        out["memory_record_ids"] = q("select id, content from memory_records order by id")
        for table in ("memory_records", "idempotency_keys"):
            out[f"{table}_columns"] = [r[0] for r in q(
                "select column_name from information_schema.columns "
                f"where table_name='{table}' order by ordinal_position")]
    engine.dispose()
    return out


def runtime_storage(url: str, tag: str) -> str:
    from openminion.modules.storage.runtime.context import build_runtime_storage

    ctx = build_runtime_storage(
        ROOT / tag / "state.db", env={}, record_backend="record.postgres",
        record_backend_options={"url": url},
    )
    try:
        return f"built, runtime version {ctx.migration_result.current_version}"
    finally:
        ctx.close()


def memory_store(url: str, tag: str, *, put: bool) -> str:
    from openminion.modules.memory.models import MemoryRecord
    from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore

    engine = sa.create_engine(url, future=True)
    store = PostgresMemoryStore(engine, database_path=ROOT / tag / "memory" / "memory.db", owns_engine=True)
    try:
        if put:
            now = "2026-10-28T00:00:00+00:00"
            store.put(MemoryRecord(id=f"rec-{tag}", scope="agent:a", type="fact", title="t", content="c", created_at=now, updated_at=now))
        return "built" + ("; 1 record put" if put else "")
    finally:
        store.close()


def a2a_audit(url: str, tag: str) -> str:
    from openminion.modules.a2a.storage.factory import build_a2a_audit_store
    from openminion.modules.storage.engine import StorageEngineConfig

    root = ROOT / tag
    store = build_a2a_audit_store(
        config=StorageEngineConfig(
            root_dir=root, sqlite_path=root / "a2a.db", fallback_root=root,
            record_backend="record.postgres", record_backend_options={"url": url},
        ),
        audit_root=root / "a2a-audit",
    )
    try:
        return "built"
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


def step(case: str, name: str, fn, url: str) -> None:
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        result = f"FAILED {type(exc).__name__}: {str(exc).splitlines()[0][:300]} (at {Path(frame.filename).name}:{frame.lineno})"
    print(f"CASE {case} | {name}: {result}")
    print(f"STATE {case} | after {name}: {json.dumps(inventory(url), sort_keys=True)}")


def main() -> int:
    url = new_db()
    step("memory_dedicated", "memory store + 1 record", lambda: memory_store(url, "m1", put=True), url)
    step("memory_dedicated", "memory store reopened", lambda: memory_store(url, "m1", put=False), url)

    url = new_db()
    step("memory_after_runtime", "runtime storage", lambda: runtime_storage(url, "r2"), url)
    step("memory_after_runtime", "memory store + 1 record", lambda: memory_store(url, "m2", put=True), url)

    url = new_db()
    step("memory_before_runtime", "memory store + 1 record", lambda: memory_store(url, "m3", put=True), url)
    step("memory_before_runtime", "runtime storage", lambda: runtime_storage(url, "r3"), url)

    url = new_db()
    step("a2a_audit_after_runtime", "runtime storage", lambda: runtime_storage(url, "r4"), url)
    step("a2a_audit_after_runtime", "a2a audit store", lambda: a2a_audit(url, "a4"), url)
    step("a2a_audit_after_runtime", "a2a audit store reopened", lambda: a2a_audit(url, "a4"), url)

    url = new_db()
    step("a2a_audit_alone", "a2a audit store", lambda: a2a_audit(url, "a5"), url)
    step("a2a_audit_alone", "runtime storage", lambda: runtime_storage(url, "r5"), url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
