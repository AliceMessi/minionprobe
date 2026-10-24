"""Memory retention and purge on items that carry evidence, PostgreSQL and SQLite.

Every case runs one retention step on a bare store (no AuditedMemoryStore, so
it does not depend on #392). Each case starts from a fresh store, a new
PostgreSQL schema or a new SQLite file (the control), seeded with three items
that the step should remove:
- `artifact-ref`: one evidence ref to an artifact (an ArtifactRef with a sha256);
- `capture-evidence`: the evidence entry that apply_capture_bundle writes on
  every captured candidate, and that promotion copies to the record;
- `no-evidence`: no evidence.
The store gets a stub artifactctl that records the artifact ref removals it is
asked for. The probe prints the step's result or exception, the three items
afterwards and the removals.

Steps (the arguments the session lifecycle and `memctl gc` pass by default):
- apply_confidence_decay: facts at 0.35, updated 28 days ago (0.15 after decay, below 0.3)
- compress_old_summaries: session summaries created 100 days ago (delete_age_days=90)
- enforce_scope_capacity: max_records=1, with a fourth fact `keep` at 0.9 in the scope
- evict_stale_insights: meta insights created 100 days ago and never hit (60 days)
- purge_soft_deleted, records: facts soft-deleted with store.delete()
- purge_soft_deleted, candidates: candidates in status `promoted`

The PostgreSQL cases need OPENMINION_TEST_POSTGRES_URL and the postgres extra;
without it they are skipped.
Run from an openminion source checkout: PYTHONPATH=src:. python probe_pg_evidence_refs.py
(PYTHONPATH="src;." on Windows)
"""

import contextlib
import datetime
import json
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

from openminion.modules.memory.models import ArtifactRef, MemoryCandidate, MemoryRecord
from openminion.modules.memory.runtime.gc import (
    apply_confidence_decay,
    compress_old_summaries,
    enforce_scope_capacity,
    evict_stale_insights,
)
from openminion.modules.memory.runtime.purge import purge_soft_deleted
from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore

POSTGRES_BASE = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()
NOW = datetime.datetime.now(datetime.timezone.utc)
ITEMS = {
    "artifact-ref": [
        ArtifactRef(ref="a" * 64, mime="text/plain", sha256="a" * 64, size_bytes=1)
    ],
    "capture-evidence": [
        ArtifactRef(
            ref="capture-1",
            mime="application/x-openminion-capture",
            sha256="b" * 64,
            size_bytes=0,
            label="assured-capture",
        )
    ],
    "no-evidence": [],
}


class RecordingArtifactCtl:
    def __init__(self):
        self.removed = []

    def ref_add(self, owner_type, owner_id, target):
        return None

    def ref_remove(self, owner_type, owner_id, target):
        self.removed.append(owner_id)

    def ref_remove_owner(self, owner_type, owner_id):
        self.removed.append(f"{owner_id} (all)")


def ago(days):
    return (NOW - datetime.timedelta(days=days)).isoformat()


def put(store, record_id, record_type, *, confidence, days, evidence):
    store.put(
        MemoryRecord(
            id=record_id,
            scope="agent:a",
            type=record_type,
            title=record_id,
            content=f"{record_id}. Second sentence.",
            created_at=ago(days),
            updated_at=ago(days),
            confidence=confidence,
            evidence_refs=evidence,
        )
    )


def seed_records(record_type, *, confidence, days):
    def seed(store):
        for item, evidence in ITEMS.items():
            put(store, item, record_type, confidence=confidence, days=days, evidence=evidence)

    return seed


def seed_capacity(store):
    for index, (item, evidence) in enumerate(ITEMS.items()):
        put(store, item, "fact", confidence=0.1 * (index + 1), days=0, evidence=evidence)
    put(store, "keep", "fact", confidence=0.9, days=0, evidence=[])


def seed_soft_deleted(store):
    seed_records("fact", confidence=0.8, days=0)(store)
    for item in ITEMS:
        store.delete(item)


def seed_promoted_candidates(store):
    for item, evidence in ITEMS.items():
        store.candidate_put(
            MemoryCandidate(
                candidate_id=item,
                session_id="s1",
                proposed_scope="agent:a",
                type="fact",
                content=item,
                evidence_refs=evidence,
                status="promoted",
            )
        )


CASES = (
    (
        "apply_confidence_decay",
        "records",
        seed_records("fact", confidence=0.35, days=28),
        lambda store: apply_confidence_decay(
            store, interval_days=7, decay_rate=0.05, min_confidence=0.3
        ),
    ),
    (
        "compress_old_summaries",
        "records",
        seed_records("session_summary", confidence=0.8, days=100),
        lambda store: compress_old_summaries(
            store, max_age_days=14, delete_age_days=90, max_summary_chars=100
        ),
    ),
    (
        "enforce_scope_capacity",
        "records",
        seed_capacity,
        lambda store: enforce_scope_capacity(store, max_records=1),
    ),
    (
        "evict_stale_insights",
        "records",
        seed_records("meta_insight", confidence=0.8, days=100),
        lambda store: evict_stale_insights(store, staleness_days=60),
    ),
    (
        "purge_soft_deleted, records",
        "records",
        seed_soft_deleted,
        lambda store: purge_soft_deleted(store),
    ),
    (
        "purge_soft_deleted, candidates",
        "candidates",
        seed_promoted_candidates,
        lambda store: purge_soft_deleted(store),
    ),
)

RECORDS_SQL = "SELECT id, confidence, is_deleted, deleted_reason FROM memory_records"
CANDIDATES_SQL = "SELECT candidate_id, status FROM memory_candidates"


def describe(kind, rows):
    described = {}
    for item in ITEMS:
        row = rows.get(item)
        if row is None:
            described[item] = "row purged"
        elif kind == "candidates":
            described[item] = f"status {row[0]}"
        else:
            confidence, is_deleted, reason = row
            if not is_deleted:
                state = "active"
            else:
                state = f"soft-deleted ({reason})" if reason else "soft-deleted"
            described[item] = f"confidence {float(confidence):.2f}, {state}"
    return described


def run_case(store, read_rows, kind, seed, step, artifactctl):
    seed(store)
    artifactctl.removed.clear()
    row = {}
    try:
        result = step(store)
        if hasattr(result, "deleted_candidates"):
            result = (
                f"GCResult(deleted_records={result.deleted_records}, "
                f"deleted_candidates={result.deleted_candidates}, ...)"
            )
        row["result"] = str(result)
    except Exception as exc:
        row["result"] = f"raised {type(exc).__name__}: {exc}"
    row.update(describe(kind, read_rows(kind)))
    row["artifact ref removals"] = sorted(artifactctl.removed)
    return row


def sqlite_cases(tmp):
    report = {}
    for number, (label, kind, seed, step) in enumerate(CASES):
        db_path = Path(tmp) / f"sqlite-{number}" / "memory.db"
        artifactctl = RecordingArtifactCtl()
        store = SQLiteMemoryStore(db_path, artifactctl=artifactctl)

        def read_rows(kind, db_path=db_path):
            sql = CANDIDATES_SQL if kind == "candidates" else RECORDS_SQL
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                return {row[0]: tuple(row[1:]) for row in conn.execute(sql)}

        report[label] = run_case(store, read_rows, kind, seed, step, artifactctl)
        store.close()
    return report


def postgres_cases(tmp):
    import sqlalchemy as sa

    from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore
    from tests.storage.postgres_test_utils import schema_url

    admin = sa.create_engine(POSTGRES_BASE, future=True)
    report = {}
    try:
        for number, (label, kind, seed, step) in enumerate(CASES):
            schema = f"probe_evidence_{uuid.uuid4().hex[:12]}"
            with admin.begin() as conn:
                conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            engine = sa.create_engine(schema_url(POSTGRES_BASE, schema), future=True)
            try:
                artifactctl = RecordingArtifactCtl()
                store = PostgresMemoryStore(
                    engine,
                    database_path=Path(tmp) / f"postgres-{number}" / "memory.db",
                    artifactctl=artifactctl,
                )

                def read_rows(kind, engine=engine):
                    sql = CANDIDATES_SQL if kind == "candidates" else RECORDS_SQL
                    with engine.connect() as conn:
                        return {row[0]: tuple(row[1:]) for row in conn.execute(sa.text(sql))}

                report[label] = run_case(store, read_rows, kind, seed, step, artifactctl)
                store.close()
            finally:
                engine.dispose()
                with admin.begin() as conn:
                    conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        admin.dispose()
    return report


def main():
    report = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        report["SQLite, bare SQLiteMemoryStore (control)"] = sqlite_cases(tmp)
        if POSTGRES_BASE:
            report["PostgreSQL, bare PostgresMemoryStore"] = postgres_cases(tmp)
        else:
            report["PostgreSQL, bare PostgresMemoryStore"] = (
                "skipped: OPENMINION_TEST_POSTGRES_URL not set"
            )
    print(json.dumps(report, indent=2))
    print(sys.version.split()[0], sys.platform)


if __name__ == "__main__":
    main()
