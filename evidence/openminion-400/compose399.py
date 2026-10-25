"""#400 composed with PR #399: the four retention helpers through AuditedMemoryStore on PostgreSQL.

Runs the #400 scenarios for the four helpers with the store wrapped in
AuditedMemoryStore, as the runtime session lifecycle passes it. Needs PR #399
(wrapper resolution) in the tree. Prints one line per case.
Run from the checkout: PYTHONPATH=src:. python compose399.py
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

import sqlalchemy as sa

from openminion.modules.memory.storage.audit import AuditedMemoryStore, InMemoryMemoryAuditSink
from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore
from tests._memory_evidence_scenarios import RecordingArtifactCtl, expected_outcome, run_case
from tests.storage.postgres_test_utils import schema_url

BASE = os.environ["OPENMINION_TEST_POSTGRES_URL"]
admin = sa.create_engine(BASE, future=True)
for case in ("apply_confidence_decay", "compress_old_summaries", "enforce_scope_capacity", "evict_stale_insights"):
    schema = f"compose_{uuid.uuid4().hex[:12]}"
    with admin.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    engine = sa.create_engine(schema_url(BASE, schema), future=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            artifactctl = RecordingArtifactCtl()
            store = PostgresMemoryStore(engine, database_path=Path(tmp) / "m.db", artifactctl=artifactctl)
            audited = AuditedMemoryStore(store, sink=InMemoryMemoryAuditSink())
            try:
                observed = run_case(case, audited, artifactctl)
                verdict = "as expected" if observed == expected_outcome(case) else f"DIFFERS: {observed}"
            except Exception as exc:
                verdict = f"raised {type(exc).__name__}: {str(exc).splitlines()[0][:100]}"
            store.close()
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    print(f"{case} through AuditedMemoryStore(PostgresMemoryStore): {verdict}")
admin.dispose()
print(sys.version.split()[0], sys.platform)
