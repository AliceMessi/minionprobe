"""#400 on the installed package: retention and purge of memory items that carry
evidence, on PostgreSQL (JSONB evidence_json) and SQLite (JSON text).

    python -I -X utf8 probe_400.py seed <work_dir> <out.json>   (run with the published 0.1.11)
    python -I -X utf8 probe_400.py run  <work_dir> <out.json>   (run with the published 0.1.12)

`seed` writes "existing history" with the installed (older) package: for each of
the seven cases, the case's items in a new SQLite file and, when
OPENMINION_TEST_POSTGRES_URL is set, a new PostgreSQL schema; it writes
<work_dir>/history-manifest.json and runs no retention step.

`run`:
  A. The shared decoder decode_evidence_ref_values: an already decoded list
     (what PostgreSQL returns for JSONB) is kept; JSON text, "[]", a JSON object,
     "" and None decode as before; text that is not JSON still raises.
  B. New evidence: each case seeds three items on a fresh bare store (no
     AuditedMemoryStore): `artifact-ref` (one artifact evidence ref),
     `capture-evidence` (the entry apply_capture_bundle writes; not an artifact
     edge) and `no-evidence`, then runs one step with the arguments the session
     lifecycle and `memctl gc` use: apply_confidence_decay,
     compress_old_summaries, enforce_scope_capacity (plus a `keep` item),
     evict_stale_insights, purge_soft_deleted of soft-deleted records, of
     promoted candidates and of rejected candidates. A recording artifactctl
     records the artifact ref edges the store removes. On PostgreSQL the stored
     evidence is also read server-side (jsonb_typeof, jsonb_array_length), and the
     evidence the store returns after the step is compared with what was written.
  C. Existing history: the same steps on the items the published 0.1.11 wrote
     (from `seed`), after first reading every item's evidence back through the
     0.1.12 store.
  D. Invalid stored JSON text still raises: a soft-deleted SQLite record whose
     evidence_json text is replaced with `not json` makes purge_soft_deleted raise
     JSONDecodeError.
  E. The four retention helpers through AuditedMemoryStore(PostgresMemoryStore)
     on items with evidence (the runtime/memctl wrapper path; needs #392 too).
     purge_soft_deleted through the wrapper on PostgreSQL is #401 and is not run.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

MODE = sys.argv[1]
WORK = Path(sys.argv[2]).resolve()
OUT = Path(sys.argv[3]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
MANIFEST = WORK / "history-manifest.json"
quiet()
clear_override_env()
PG_BASE = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()

extra: dict[str, Any] = {"mode": MODE}
admin = None
if PG_BASE:
    import psycopg
    import sqlalchemy as sa

    admin = sa.create_engine(PG_BASE, future=True)
    with admin.connect() as _conn:
        extra["postgres_server_version"] = _conn.execute(sa.text("SHOW server_version")).scalar_one()
        extra["postgres_version"] = _conn.execute(sa.text("SELECT version()")).scalar_one()
    extra["sqlalchemy"] = sa.__version__
    extra["psycopg"] = psycopg.__version__
else:
    extra["postgres"] = "not run: OPENMINION_TEST_POSTGRES_URL is not set"
info = banner("probe_400", **extra)

from openminion.modules.memory.models import ArtifactRef, MemoryCandidate, MemoryRecord  # noqa: E402
from openminion.modules.memory.runtime.gc import (  # noqa: E402
    apply_confidence_decay,
    compress_old_summaries,
    enforce_scope_capacity,
    evict_stale_insights,
)
from openminion.modules.memory.runtime.purge import decode_evidence_ref_values, purge_soft_deleted  # noqa: E402
from openminion.modules.memory.storage.audit import AuditedMemoryStore, InMemoryMemoryAuditSink  # noqa: E402
from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore  # noqa: E402

c = Checks()
observed: dict[str, Any] = {}

SCOPE = "agent:evidence"
SHA = "a" * 64
EVIDENCE = {
    "artifact-ref": [ArtifactRef(ref=SHA, mime="text/plain", sha256=SHA, size_bytes=1)],
    "capture-evidence": [ArtifactRef(ref="capture-1", mime="application/x-openminion-capture", sha256="b" * 64,
                                     size_bytes=0, label="assured-capture")],
    "no-evidence": [],
}
ITEMS = tuple(EVIDENCE)
CASES = ("apply_confidence_decay", "compress_old_summaries", "enforce_scope_capacity", "evict_stale_insights",
         "purge_soft_deleted records", "purge_soft_deleted promoted candidates",
         "purge_soft_deleted rejected candidates")
RETENTION = CASES[:4]
REMOVED = [["memory", "artifact-ref", SHA]]


class RecordingArtifactCtl:
    def __init__(self) -> None:
        self.removed: list[tuple[str, str, str]] = []

    def ref_add(self, owner_type: str, owner_id: str, target: str) -> None:
        return None

    def ref_remove(self, owner_type: str, owner_id: str, target: str) -> None:
        self.removed.append((owner_type, owner_id, target))

    def ref_remove_owner(self, owner_type: str, owner_id: str) -> None:
        self.removed.append((owner_type, owner_id, "(all)"))


def days_ago(days: int) -> str:
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()


def put_record(store: Any, rid: str, rtype: str, *, confidence: float, age_days: int, evidence: list) -> None:
    when = days_ago(age_days)
    store.put(MemoryRecord(id=rid, scope=SCOPE, type=rtype, title=rid, content=f"{rid}. Second sentence.",
                           created_at=when, updated_at=when, confidence=confidence, evidence_refs=evidence))


def seed(case: str, store: Any) -> None:
    def records(rtype: str, *, confidence: float, age_days: int) -> None:
        for item in ITEMS:
            put_record(store, item, rtype, confidence=confidence, age_days=age_days, evidence=EVIDENCE[item])

    if case == "apply_confidence_decay":
        records("fact", confidence=0.35, age_days=28)
    elif case == "compress_old_summaries":
        records("session_summary", confidence=0.8, age_days=100)
    elif case == "enforce_scope_capacity":
        for index, item in enumerate(ITEMS):
            put_record(store, item, "fact", confidence=0.1 * (index + 1), age_days=0, evidence=EVIDENCE[item])
        put_record(store, "keep", "fact", confidence=0.9, age_days=0, evidence=[])
    elif case == "evict_stale_insights":
        records("meta_insight", confidence=0.8, age_days=100)
    elif case == "purge_soft_deleted records":
        records("fact", confidence=0.8, age_days=0)
        for item in ITEMS:
            store.delete(item)
    else:
        status = case.split()[1]
        for item in ITEMS:
            store.candidate_put(MemoryCandidate(candidate_id=item, session_id="s1", proposed_scope=SCOPE, type="fact",
                                                content=item, evidence_refs=EVIDENCE[item], status=status))


def step(case: str, target: Any) -> Any:
    if case == "apply_confidence_decay":
        return apply_confidence_decay(target, interval_days=7, decay_rate=0.05, min_confidence=0.3)
    if case == "compress_old_summaries":
        return compress_old_summaries(target, max_age_days=14, delete_age_days=90, max_summary_chars=100)
    if case == "enforce_scope_capacity":
        return enforce_scope_capacity(target, max_records=1)
    if case == "evict_stale_insights":
        return evict_stale_insights(target, staleness_days=60)
    result = purge_soft_deleted(target)
    return (result.deleted_records, result.deleted_candidates)


def item_ids(case: str) -> list[str]:
    return list(ITEMS) + (["keep"] if case == "enforce_scope_capacity" else [])


def state(case: str, store: Any, item: str) -> Any:
    if case.endswith(" candidates"):
        cand = store.candidate_get(item)
        return "purged" if cand is None else str(cand.status)
    rec = store.get(item)
    if rec is None:
        return "purged"
    return [round(float(rec.confidence), 2), rec.deleted_reason if rec.is_deleted else "active"]


def evidence_read(case: str, store: Any, item: str) -> Any:
    obj = store.candidate_get(item) if case.endswith(" candidates") else store.get(item)
    if obj is None:
        return None
    return [dict(sorted(vars(ref).items())) for ref in obj.evidence_refs]


def written(item: str) -> list[dict[str, Any]]:
    return [dict(sorted(vars(ref).items())) for ref in EVIDENCE.get(item, [])]


def expected(case: str) -> dict[str, Any]:
    soft = {"apply_confidence_decay": ([3, 3], 0.15, "confidence_below_threshold"),
            "compress_old_summaries": ([0, 3], 0.8, "summary_retention"),
            "evict_stale_insights": (3, 0.8, "stale_insight")}
    if case in soft:
        result, confidence, reason = soft[case]
        items: dict[str, Any] = {item: [confidence, reason] for item in ITEMS}
    elif case == "enforce_scope_capacity":
        result = {SCOPE: 3}
        items = {"artifact-ref": [0.1, "scope_capacity"], "capture-evidence": [0.2, "scope_capacity"],
                 "no-evidence": [0.3, "scope_capacity"], "keep": [0.9, "active"]}
    else:
        result = [3, 0] if case == "purge_soft_deleted records" else [0, 3]
        items = {item: "purged" for item in ITEMS}
    return {"result": result, "items": items, "artifact ref removals": REMOVED}


def norm(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


class PgSchema:
    def __init__(self, label: str, name: str | None = None) -> None:
        self.schema = name or f"probe400_{label}_{uuid.uuid4().hex[:10]}"
        if name is None:
            with admin.begin() as conn:  # type: ignore[union-attr]
                conn.execute(sa.text(f'CREATE SCHEMA "{self.schema}"'))
        sep = "&" if "?" in PG_BASE else "?"
        self.url = f"{PG_BASE}{sep}options={quote_plus('-csearch_path=' + self.schema)}"
        self.engine = sa.create_engine(self.url, future=True)

    def store(self, artifactctl: Any) -> Any:
        from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore

        return PostgresMemoryStore(self.engine, database_path=WORK / f"pgdb-{uuid.uuid4().hex[:8]}" / "memory.db",
                                   artifactctl=artifactctl)

    def server_evidence(self, case: str) -> dict[str, Any]:
        if case.endswith(" candidates"):
            sql = ("SELECT candidate_id, jsonb_typeof(evidence_json), jsonb_array_length(evidence_json) "
                   "FROM memory_candidates ORDER BY candidate_id")
        else:
            sql = ("SELECT id, jsonb_typeof(evidence_json), jsonb_array_length(evidence_json) "
                   "FROM memory_records ORDER BY id")
        with self.engine.connect() as conn:
            return {r[0]: [r[1], r[2]] for r in conn.execute(sa.text(sql))}

    def drop(self) -> None:
        self.engine.dispose()
        with admin.begin() as conn:  # type: ignore[union-attr]
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))


def run_step(case: str, store: Any, artifactctl: RecordingArtifactCtl, target: Any | None = None) -> dict[str, Any]:
    artifactctl.removed.clear()
    try:
        result = step(case, store if target is None else target)
    except Exception as exc:  # noqa: BLE001
        result = f"raised {type(exc).__name__}: {exc}"
    return norm({"result": result, "items": {i: state(case, store, i) for i in item_ids(case)},
                 "artifact ref removals": sorted(artifactctl.removed)})


# ======================================================================== seed
if MODE == "seed":
    manifest: dict[str, Any] = {"written_by": info.get("openminion.__version__"), "sqlite": {}, "postgres": {}}
    for case in CASES:
        db = WORK / "history-sqlite" / f"{CASES.index(case)}" / "memory.db"
        store = SQLiteMemoryStore(db, artifactctl=RecordingArtifactCtl())
        seed(case, store)
        store.close()
        manifest["sqlite"][case] = str(db)
        if PG_BASE:
            pg = PgSchema("hist")
            store = pg.store(RecordingArtifactCtl())
            seed(case, store)
            store.close()
            manifest["postgres"][case] = {"schema": pg.schema, "server_evidence": pg.server_evidence(case)}
            pg.engine.dispose()
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    c.check("seed: history written by the installed package", True,
            {"version": manifest["written_by"], "sqlite_cases": len(manifest["sqlite"]),
             "postgres_cases": len(manifest["postgres"])})
    observed["manifest"] = {**manifest, "sqlite": {k: "<WORK>/history-sqlite/..." for k in manifest["sqlite"]}}
    s = c.summary()
    write_result(OUT, {"info": info, "observed": observed, "checks": c.items, "summary": s})
    print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
    sys.exit(0)

# ======================================================================== run
# A. decoder
ART = {"label": None, "mime": "text/plain", "ref": SHA, "sha256": SHA, "size_bytes": 1}
CAP = {"label": "assured-capture", "mime": "application/x-openminion-capture", "ref": "capture-1",
       "sha256": "b" * 64, "size_bytes": 0}
decoder_cases = [
    ("already decoded list [artifact, capture] (PostgreSQL JSONB)", [ART, CAP], [ART, CAP]),
    ("already decoded list [sha]", [SHA], [SHA]),
    ("already decoded empty list", [], []),
    ("JSON text [artifact, capture] (SQLite)", json.dumps([ART, CAP]), [ART, CAP]),
    ("JSON text []", "[]", []),
    ("JSON text of an object", json.dumps(ART), []),
    ("empty string", "", []),
    ("None", None, []),
]
for label, value, want in decoder_cases:
    try:
        got: Any = decode_evidence_ref_values(value)
    except Exception as exc:  # noqa: BLE001
        got = f"raised {type(exc).__name__}: {exc}"
    c.check(f"A. decode_evidence_ref_values({label})", got == want, got, want)
try:
    decode_evidence_ref_values("not json")
    got = "returned"
except json.JSONDecodeError as exc:
    got = f"raised JSONDecodeError: {exc}"
except Exception as exc:  # noqa: BLE001
    got = f"raised {type(exc).__name__}: {exc}"
c.check("A. decode_evidence_ref_values('not json') still raises JSONDecodeError", got.startswith("raised JSONDecodeError"),
        got)

backends = ["sqlite"] + (["postgres"] if PG_BASE else [])

# B. new evidence, bare stores
for backend in backends:
    for case in CASES:
        label = f"B.{backend}: {case}"
        artifactctl = RecordingArtifactCtl()
        pg = PgSchema("new") if backend == "postgres" else None
        store = (pg.store(artifactctl) if pg is not None
                 else SQLiteMemoryStore(WORK / "new-sqlite" / f"{CASES.index(case)}" / "memory.db",
                                        artifactctl=artifactctl))
        try:
            seed(case, store)
            row: dict[str, Any] = {}
            if pg is not None:
                row["server_evidence_before"] = pg.server_evidence(case)
            row["outcome"] = run_step(case, store, artifactctl)
            row["evidence_after"] = {i: evidence_read(case, store, i) for i in ITEMS}
            observed[label] = row
            c.check(f"{label}: step result, item states and artifact ref removals", row["outcome"] == expected(case),
                    row["outcome"], expected(case))
            if not case.startswith("purge_soft_deleted"):
                c.check(f"{label}: evidence values read back unchanged after the step (not stringified or dropped)",
                        row["evidence_after"] == {i: written(i) for i in ITEMS}, row["evidence_after"])
            if pg is not None:
                want_server = {"artifact-ref": ["array", 1], "capture-evidence": ["array", 1],
                               "no-evidence": ["array", 0]}
                if case == "enforce_scope_capacity":
                    want_server["keep"] = ["array", 0]
                c.check(f"{label}: evidence_json is a nonempty JSONB array on the server before the step",
                        row["server_evidence_before"] == want_server, row["server_evidence_before"], want_server)
        finally:
            store.close()
            if pg is not None:
                pg.drop()

# C. existing history written by the published 0.1.11
if MANIFEST.exists():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    observed["C.history_written_by"] = manifest.get("written_by")
    c.check("C. history was written by the published 0.1.11", manifest.get("written_by") == "0.1.11",
            manifest.get("written_by"))
    for backend in backends:
        source = manifest["sqlite"] if backend == "sqlite" else manifest["postgres"]
        for case in CASES:
            label = f"C.{backend} (history from 0.1.11): {case}"
            if case not in source:
                c.check(f"{label}: present in the history manifest", False, "missing")
                continue
            artifactctl = RecordingArtifactCtl()
            pg = PgSchema("hist", name=source[case]["schema"]) if backend == "postgres" else None
            store = pg.store(artifactctl) if pg is not None else SQLiteMemoryStore(Path(source[case]),
                                                                                   artifactctl=artifactctl)
            try:
                row = {}
                try:
                    row["evidence_before"] = {i: evidence_read(case, store, i) for i in ITEMS}
                except Exception as exc:  # noqa: BLE001
                    row["evidence_before"] = f"raised {type(exc).__name__}: {exc}"
                if pg is not None:
                    row["server_evidence_seeded_by_0111"] = source[case]["server_evidence"]
                row["outcome"] = run_step(case, store, artifactctl)
                observed[label] = row
                c.check(f"{label}: 0.1.11-written evidence reads back unchanged through 0.1.12 (no decoder error)",
                        row["evidence_before"] == {i: written(i) for i in ITEMS}, row["evidence_before"])
                c.check(f"{label}: step result, item states and artifact ref removals", row["outcome"] == expected(case),
                        row["outcome"], expected(case))
            finally:
                store.close()
                if pg is not None:
                    pg.drop()
else:
    observed["C"] = "not run: no history manifest (run `seed` with 0.1.11 first)"
    c.check("C. existing history from 0.1.11 available", False, "no history manifest")

# D. invalid stored JSON text still raises (SQLite)
db = WORK / "invalid-sqlite" / "memory.db"
artifactctl = RecordingArtifactCtl()
store = SQLiteMemoryStore(db, artifactctl=artifactctl)
try:
    put_record(store, "bad", "fact", confidence=0.8, age_days=0, evidence=EVIDENCE["artifact-ref"])
    store.delete("bad")
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute("UPDATE memory_records SET evidence_json = 'not json' WHERE id = 'bad'")
        conn.commit()
    try:
        res = purge_soft_deleted(store)
        got = f"returned deleted_records={res.deleted_records}"
    except json.JSONDecodeError as exc:
        got = f"raised JSONDecodeError: {exc}"
    except Exception as exc:  # noqa: BLE001
        got = f"raised {type(exc).__name__}: {exc}"
    observed["D.sqlite invalid evidence_json"] = got
    c.check("D. SQLite: purge_soft_deleted on a record whose stored evidence_json is not JSON raises JSONDecodeError",
            got.startswith("raised JSONDecodeError"), got)
finally:
    store.close()

# E. the four retention helpers through AuditedMemoryStore(PostgresMemoryStore), with evidence
if PG_BASE:
    for case in RETENTION:
        label = f"E.postgres audited wrapper: {case}"
        artifactctl = RecordingArtifactCtl()
        pg = PgSchema("wrap")
        store = pg.store(artifactctl)
        sink = InMemoryMemoryAuditSink()
        try:
            seed(case, store)
            outcome = run_step(case, store, artifactctl, target=AuditedMemoryStore(store, sink=sink))
            observed[label] = {"outcome": outcome, "audit_events": len(sink.events)}
            c.check(f"{label}: same outcome as on the bare store, no audit events",
                    outcome == expected(case) and sink.events == [], observed[label], expected(case))
        finally:
            store.close()
            pg.drop()
    observed["E.purge_soft_deleted through the wrapper"] = "not run: #401, not in 0.1.12"
else:
    observed["E"] = "not run: OPENMINION_TEST_POSTGRES_URL is not set"

if admin is not None:
    admin.dispose()
s = c.summary()
write_result(OUT, {"info": info, "backends": backends, "observed": observed, "checks": c.items, "summary": s})
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']} backends={backends}")
sys.exit(0 if c.all_pass else 1)
