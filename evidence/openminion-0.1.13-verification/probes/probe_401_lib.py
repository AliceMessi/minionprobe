"""Helpers for probe_401.py: per-case memctl environments, the seed, and
server-side reads of the memory rows, candidates, evidence and artifact edges
on SQLite and PostgreSQL. Installed package only (no repo test fixtures)."""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

SCOPE = "agent:a"
PG_BASE = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()

# The memctl config sections upstream's typed-config memctl gc test uses (#401);
# retention keeps the defaults (decay 0.05 per 7 days, eviction below 0.3).
MEMCTL_SECTIONS: dict[str, Any] = {
    "defaults": {"confidence": {"user_said": 0.7, "tool_output": 0.8, "agent_inferred": 0.4, "validated": 0.95,
                                "imported": 0.9}},
    "promotion": {"require_approval_for_global": True, "auto_promote_agent_procedures": True,
                  "allowlisted_auto_rules": []},
    "retrieval": {"max_results": 25, "min_confidence_default": 0.55, "pin_first": True},
    "retention": {"enable_soft_delete": True, "gc_enabled": True, "gc_batch_size": 500},
}
FIXED_IDS = ("old", "low", "deleted", "fresh", "pref-v1", "bad")
CANDIDATES = (("c-proposed", "proposed"), ("c-approved", "approved"), ("c-rejected", "rejected"),
              ("c-promoted", "promoted"))


def redact(text: str) -> str:
    if PG_BASE:
        text = text.replace(PG_BASE, "<PGURL>")
    return re.sub(r"://[^/@\s]+@", "://<credentials>@", text)


def days_ago(days: int) -> str:
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()


@contextlib.contextmanager
def environ(values: dict[str, str]):
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Case:
    """One fresh memctl environment: home, data root (artifact store, audit
    database) and a typed memctl config file selecting the case's store."""

    def __init__(self, root: Path, backend: "Backend") -> None:
        self.root = root
        self.backend = backend
        (root / "home").mkdir(parents=True, exist_ok=True)
        (root / "data" / "memory").mkdir(parents=True, exist_ok=True)
        self.config = root / "memory.yaml"
        self.write_config(backend.store_config())

    def write_config(self, store_config: dict[str, Any]) -> None:
        config = {"version": 1, "memctl": {**MEMCTL_SECTIONS, "store": store_config}}
        self.config.write_text(json.dumps(config), encoding="utf-8")  # JSON is valid YAML

    @property
    def env(self) -> dict[str, str]:
        home = str(self.root / "home")
        return {"MEMCTL_CONFIG": str(self.config), "OPENMINION_HOME": home,
                "OPENMINION_DATA_ROOT": str(self.root / "data"), "HOME": home, "USERPROFILE": home}

    def audit_events(self) -> Any:
        found = sorted(self.root.glob("data/**/*.audit.db"))
        if not found:
            return "no audit database"
        out = {}
        for path in found:
            with closing(sqlite3.connect(str(path))) as conn:
                try:
                    out[path.name] = [r[0] for r in conn.execute("SELECT event_type FROM memory_audit_events")]
                except sqlite3.OperationalError as exc:
                    out[path.name] = f"no events table ({exc})"
        return out


class Artifacts:
    """The default artifact store memctl's stores resolve from OPENMINION_HOME /
    OPENMINION_DATA_ROOT; the probe ingests real artifacts there and reads the
    reference edges from its SQLite index."""

    def __init__(self, case: Case) -> None:
        from openminion.modules.artifact.refs import create_default_artifactctl

        with environ(case.env):
            self.ctl = create_default_artifactctl()
        self.index = Path(self.ctl.config.index.sqlite_path)
        self.under_case_root = str(self.index.resolve()).startswith(str(case.root.resolve()))

    def evidence(self, name: str) -> Any:
        from openminion.modules.memory.models import ArtifactRef

        ref = self.ctl.ingest_bytes(f"evidence for {name}".encode(), mime="text/plain", original_name=f"{name}.txt")
        return ArtifactRef(ref=ref.ref, mime="text/plain", sha256=ref.sha256, size_bytes=ref.size_bytes)

    def edges(self) -> dict[str, str]:
        with closing(sqlite3.connect(str(self.index))) as conn:
            rows = conn.execute("SELECT owner_type, owner_id, deleted_at FROM reference_edges ORDER BY owner_id").fetchall()
        return {f"{t}:{o}": ("active" if d is None else "removed") for t, o, d in rows}

    def close(self) -> None:
        self.ctl.close()


def seed(store: Any, artifacts: Artifacts) -> dict[str, Any]:
    from openminion.modules.memory.models import MemoryCandidate, MemoryRecord

    refs = {name: artifacts.evidence(name) for name in ("old", "low", "deleted", "c-rejected")}

    def put(rid: str, *, confidence: float, age: int, evidence: list, key: str | None = None) -> None:
        when = days_ago(age)
        store.put(MemoryRecord(id=rid, scope=SCOPE, type="fact", key=key, title=rid, content=f"{rid} content",
                               created_at=when, updated_at=when, confidence=confidence, evidence_refs=evidence))

    put("old", confidence=0.8, age=28, evidence=[refs["old"]])        # decays to 0.6, kept
    put("low", confidence=0.35, age=28, evidence=[refs["low"]])       # decays below 0.3: soft-deleted, then purged
    put("deleted", confidence=0.8, age=28, evidence=[refs["deleted"]])
    store.delete("deleted")                                            # soft-deleted: purged
    put("fresh", confidence=0.9, age=0, evidence=[])                   # untouched
    put("pref-v1", confidence=0.9, age=0, evidence=[], key="pref:indent")
    store.upsert(SCOPE, "fact", "pref:indent", {"title": "pref v2", "content": "pref v2 content"})  # v1 superseded, kept
    for cid, status in CANDIDATES:
        store.candidate_put(MemoryCandidate(candidate_id=cid, session_id="s1", proposed_scope=SCOPE, type="fact",
                                            content=cid, status=status,
                                            evidence_refs=[refs["c-rejected"]] if cid == "c-rejected" else []))
    return refs


def label_rows(rows: list[tuple]) -> dict[str, Any]:
    out = {}
    for rid, key, confidence, is_deleted, reason in rows:
        label = rid if rid in FIXED_IDS else (f"{key} (upserted)" if key else rid)
        out[label] = [round(float(confidence), 3), (reason or "soft-deleted") if is_deleted else "active"]
    return dict(sorted(out.items()))


class Backend:
    name = "?"

    def store(self, artifactctl: Any) -> Any:
        raise NotImplementedError

    def store_config(self) -> dict[str, Any]:
        raise NotImplementedError

    def query(self, sql: str) -> list[tuple]:
        raise NotImplementedError

    def state(self) -> dict[str, Any]:
        """Server-side read of the memory rows and candidates."""
        rows = self.query("SELECT id, key, confidence, is_deleted, deleted_reason FROM memory_records")
        cands = self.query("SELECT candidate_id, status FROM memory_candidates")
        return {"records": label_rows(rows), "candidates": dict(sorted((c, str(s)) for c, s in cands)),
                "memory_records rows": len(rows), "memory_candidates rows": len(cands)}

    def drop(self) -> None:
        pass


class SqliteBackend(Backend):
    name = "sqlite"

    def __init__(self, root: Path) -> None:
        self.db = root / "data" / "memory" / "memory.db"

    def store(self, artifactctl: Any) -> Any:
        from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore

        return SQLiteMemoryStore(self.db, artifactctl=artifactctl)

    def store_config(self) -> dict[str, Any]:
        return {"backend": "sqlite", "sqlite_path": str(self.db)}

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with closing(sqlite3.connect(str(self.db))) as conn:
            return [tuple(r) for r in conn.execute(sql, params).fetchall()]

    def execute(self, sql: str) -> None:
        with closing(sqlite3.connect(str(self.db))) as conn:
            conn.executescript(sql)
            conn.commit()

    def evidence(self, rid: str) -> Any:
        rows = self.query("SELECT evidence_json FROM memory_records WHERE id = ?", (rid,))
        if not rows:
            return "row purged"
        value = json.loads(rows[0][0] or "[]")
        return ["text", len(value)]

    def block_purge(self) -> None:
        self.execute("CREATE TRIGGER probe_block_purge BEFORE DELETE ON memory_records "
                     "BEGIN SELECT RAISE(ABORT, 'probe: purge blocked'); END;")

    def corrupt_evidence(self, rid: str) -> None:
        self.execute(f"UPDATE memory_records SET evidence_json = 'not json' WHERE id = '{rid}'")


class PgBackend(Backend):
    name = "postgres"

    def __init__(self, root: Path, admin: Any) -> None:
        import sqlalchemy as sa

        self.sa = sa
        self.root = root
        self.admin = admin
        self.schema = f"probe401_{uuid.uuid4().hex[:10]}"
        with admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{self.schema}"'))
        sep = "&" if "?" in PG_BASE else "?"
        self.url = f"{PG_BASE}{sep}options={quote_plus('-csearch_path=' + self.schema)}"
        self.engine = sa.create_engine(self.url, future=True)

    def store(self, artifactctl: Any) -> Any:
        from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore

        return PostgresMemoryStore(self.engine, database_path=self.root / "data" / "memory" / "memory.db",
                                   artifactctl=artifactctl)

    def store_config(self) -> dict[str, Any]:
        return {"backend": "postgres", "postgres": {"url": self.url}}

    def query(self, sql: str, params: dict | None = None) -> list[tuple]:
        with self.engine.connect() as conn:
            return [tuple(r) for r in conn.execute(self.sa.text(sql), params or {})]

    def execute(self, sql: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(self.sa.text(sql))

    def evidence(self, rid: str) -> Any:
        rows = self.query("SELECT jsonb_typeof(evidence_json), jsonb_array_length(evidence_json) "
                          "FROM memory_records WHERE id = :id", {"id": rid})
        return list(rows[0]) if rows else "row purged"

    def schema_check(self) -> dict[str, Any]:
        rows = self.query("SELECT current_schema(), (SELECT count(*) FROM information_schema.tables "
                          "WHERE table_schema = current_schema() AND table_name = 'memory_records')")
        return {"current_schema is the probe's own": rows[0][0] == self.schema, "memory_records table in it": rows[0][1]}

    def block_purge(self) -> None:
        self.execute(f'CREATE FUNCTION "{self.schema}".probe_block_purge() RETURNS trigger LANGUAGE plpgsql AS '
                     "$$ BEGIN RAISE EXCEPTION 'probe: purge blocked'; END $$")
        self.execute(f'CREATE TRIGGER probe_block_purge BEFORE DELETE ON "{self.schema}".memory_records '
                     f'FOR EACH ROW EXECUTE FUNCTION "{self.schema}".probe_block_purge()')

    def drop(self) -> None:
        self.engine.dispose()
        with self.admin.begin() as conn:
            conn.execute(self.sa.text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
