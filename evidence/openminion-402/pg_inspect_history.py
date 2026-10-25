"""build_inspect_payload's history summary on PostgreSQL, bare and behind AuditedMemoryStore.

Seeds a keyed fact with two versions in a fresh PostgreSQL schema and prints
history_summary for a MemoryService over the bare PostgresMemoryStore and over
AuditedMemoryStore(PostgresMemoryStore). The summary is SQLite-only, so both
are expected to stay null (#402 keeps that behaviour).
Run from the checkout: PYTHONPATH=src:. python pg_inspect_history.py
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

import sqlalchemy as sa

from openminion.modules.memory.diagnostics.cli import build_inspect_payload
from openminion.modules.memory.models import MemoryRecord
from openminion.modules.memory.service import MemoryService
from openminion.modules.memory.storage.audit import AuditedMemoryStore, InMemoryMemoryAuditSink
from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore
from tests.storage.postgres_test_utils import schema_url

BASE = os.environ["OPENMINION_TEST_POSTGRES_URL"]
admin = sa.create_engine(BASE, future=True)
schema = f"inspect_{uuid.uuid4().hex[:12]}"
with admin.begin() as conn:
    conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
engine = sa.create_engine(schema_url(BASE, schema), future=True)
try:
    with tempfile.TemporaryDirectory() as tmp:
        store = PostgresMemoryStore(engine, database_path=Path(tmp) / "m.db", artifactctl=None)
        store.put(MemoryRecord(id="v1", scope="agent:a", type="fact", key="pref:indent",
                               title="Prefer tabs", content="I prefer tabs.",
                               created_at="2026-10-01T00:00:00+00:00",
                               updated_at="2026-10-01T00:00:00+00:00"))
        store.upsert("agent:a", "fact", "pref:indent", {"title": "Prefer spaces", "content": "I prefer spaces."})
        print("history depth for the key:", len(store.history("agent:a", "fact", "pref:indent")))
        trace = Path(tmp) / "trace.jsonl"
        trace.write_text("", encoding="utf-8")
        for label, svc_store in (("bare PostgresMemoryStore", store),
                                 ("AuditedMemoryStore(PostgresMemoryStore)", AuditedMemoryStore(store, sink=InMemoryMemoryAuditSink()))):
            payload = build_inspect_payload(service=MemoryService(svc_store), scope="agent:a",
                                            db_path=Path(tmp) / "m.db", trace_file=trace)
            print(f"{label}: history_summary={payload['history_summary']!r}, "
                  f"stats.active_record_count={(payload['stats'] or {}).get('active_record_count')}")
        store.close()
finally:
    engine.dispose()
    with admin.begin() as conn:
        conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    admin.dispose()
print(sys.version.split()[0], sys.platform)
