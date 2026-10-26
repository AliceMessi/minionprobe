"""#392 on the installed package: memory retention in the runtime session lifecycle
with a PostgreSQL memory store behind the runtime's AuditedMemoryStore.

  A. SDK session lifecycle (SQLite control, and PostgreSQL when
     OPENMINION_TEST_POSTGRES_URL is set). An echo runtime whose memory config is
     registered on the ConfigManager (PostgreSQL store with its own schema, as
     upstream's integration test does; max_records_per_scope=3) runs two turns of
     one session. The store the runtime creates is seeded when it is created
     (wrapping services.runtime.memory.resolve_memory_backend): records for
     confidence decay, summary compression and deletion, scope capacity and stale
     insight eviction. Several of them carry evidence: a real artifact ingested
     into the runtime's own ArtifactCtl (an artifact reference edge) and a
     capture-evidence entry (not an artifact edge). After the first turn one more
     stale insight is added; the second turn must not run the lifecycle again.
     Observed: every lifecycle helper call (name, store type, ok/raised), every
     WARNING log record (the `memory.session_lifecycle failed` warning and any
     `_connect` text), each record's confidence/deleted_reason/evidence after the
     turns, the artifact index's reference edges per memory owner, and on
     PostgreSQL the same rows read server-side from the schema.
  B. The four retention helpers on seeded records, called on the bare store
     ("direct") and on AuditedMemoryStore(store, sink=InMemoryMemoryAuditSink())
     ("audited"), on SQLite and on PostgreSQL: the records each helper should
     change, the ones it must not, and no audit events (GC writes are not audited).

Usage: python -I -X utf8 probe_392.py <work_dir> <out.json>
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import json
import logging
import os
import sqlite3
import sys
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, echo_config, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
clear_override_env()
logging.disable(logging.INFO)  # keep WARNING and above: the lifecycle warning is a WARNING
PG_BASE = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()

extra: dict[str, Any] = {}
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
info = banner("probe_392", **extra)

from openminion import Agent, APIRuntime  # noqa: E402
from openminion.base.config import ConfigManager  # noqa: E402
from openminion.modules.memory.config import from_base_config  # noqa: E402
from openminion.modules.memory.models import ArtifactRef, MemoryRecord  # noqa: E402
from openminion.modules.memory.runtime import session_lifecycle  # noqa: E402
from openminion.modules.memory.runtime.gc import (  # noqa: E402
    apply_confidence_decay,
    compress_old_summaries,
    enforce_scope_capacity,
    evict_stale_insights,
)
from openminion.modules.memory.storage.audit import AuditedMemoryStore, InMemoryMemoryAuditSink  # noqa: E402
from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore  # noqa: E402
from openminion.services.runtime import memory as runtime_memory  # noqa: E402

c = Checks()
observed: dict[str, Any] = {}
LIFECYCLE_HELPERS = ("apply_confidence_decay", "compress_old_summaries_gc", "enforce_scope_capacity", "evict_stale_insights")
CAPTURE = ArtifactRef(ref="capture-1", mime="application/x-openminion-capture", sha256="b" * 64, size_bytes=0,
                      label="assured-capture")


# ---------------------------------------------------------------- helpers
def days_ago(days: int) -> str:
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()


def record(rid: str, *, scope: str = "agent:retention", rtype: str = "fact", confidence: float = 0.8,
           age_days: int = 1, content: Any = "content", last_hit_days_ago: int | None = None,
           evidence: list[ArtifactRef] | None = None) -> MemoryRecord:
    when = days_ago(age_days)
    return MemoryRecord(id=rid, scope=scope, type=rtype, title=rid, content=content, created_at=when, updated_at=when,
                        confidence=confidence, evidence_refs=list(evidence or []),
                        last_hit_at=days_ago(last_hit_days_ago) if last_hit_days_ago is not None else None)


def summary(rid: str, *, age_days: int, text: str, scope: str = "agent:retention",
            evidence: list[ArtifactRef] | None = None) -> MemoryRecord:
    return record(rid, scope=scope, rtype="session_summary", confidence=1.0, age_days=age_days, evidence=evidence,
                  content={"decisions": ["decided to use pytest"], "open_questions": [], "corrections": [],
                           "topic_keywords": ["pytest"], "turn_count": 4, "summary_text": text})


def deleted_reason(store: Any, rid: str) -> str | None:
    rec = store.get(rid)
    if rec is None:
        return "MISSING"
    return rec.deleted_reason if rec.is_deleted else None


def evidence_of(store: Any, rid: str) -> list[dict[str, Any]]:
    rec = store.get(rid)
    return [] if rec is None else [dict(sorted(vars(item).items())) for item in rec.evidence_refs]


class PgSchema:
    def __init__(self, label: str) -> None:
        self.schema = f"probe392_{label}_{uuid.uuid4().hex[:10]}"
        with admin.begin() as conn:  # type: ignore[union-attr]
            conn.execute(sa.text(f'CREATE SCHEMA "{self.schema}"'))
        sep = "&" if "?" in PG_BASE else "?"
        self.url = f"{PG_BASE}{sep}options={quote_plus('-csearch_path=' + self.schema)}"

    def rows(self, sql: str) -> list[tuple]:
        engine = sa.create_engine(self.url, future=True)
        try:
            with engine.connect() as conn:
                return [tuple(r) for r in conn.execute(sa.text(sql))]
        finally:
            engine.dispose()

    def drop(self) -> None:
        with admin.begin() as conn:  # type: ignore[union-attr]
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))


def edges(artifactctl: Any, sha: str) -> dict[str, str]:
    """Reference edges (owner_type 'memory') to `sha` in the runtime's artifact index."""
    path = Path(str(artifactctl.config.index.sqlite_path))
    with closing(sqlite3.connect(str(path))) as conn:
        rows = conn.execute(
            "SELECT owner_id, deleted_at FROM reference_edges WHERE owner_type = 'memory' AND sha256 = ?", (sha,)
        ).fetchall()
    return {owner: ("removed" if deleted_at else "active") for owner, deleted_at in sorted(rows)}


# ---------------------------------------------------------------- A. SDK session lifecycle
def sdk_lifecycle(backend: str) -> None:
    label = f"A.{backend}"
    root = WORK / f"sdk-{backend}"
    pg = PgSchema("sdk") if backend == "postgres" else None
    stores: list[Any] = []
    seeded: dict[str, Any] = {}
    calls: list[tuple[str, str, str]] = []
    warnings: list[str] = []
    resolve_backend = runtime_memory.resolve_memory_backend
    originals = {name: getattr(session_lifecycle, name) for name in LIFECYCLE_HELPERS}

    def seeded_backend(**kwargs: Any) -> Any:
        resolved = resolve_backend(**kwargs)
        store = resolved.store
        artifactctl = store._resolve_artifactctl()
        art = artifactctl.ingest_bytes(b"probe 392 evidence artifact\n", mime="text/plain", original_name="ev392.txt")
        seeded["sha"] = art.sha256
        seeded["artifactctl"] = artifactctl
        ref = ArtifactRef(ref=art.ref, mime="text/plain", sha256=art.sha256, size_bytes=art.size_bytes)
        seeded["ref"] = ref
        text = "Decided to use pytest. Actually, wrong fixture scope."
        store.put(record("decays", scope="agent:decay", confidence=0.8, age_days=28, evidence=[ref]))
        store.put(record("decay-evicted", scope="agent:decay", confidence=0.35, age_days=28, evidence=[ref, CAPTURE]))
        store.put(summary("summary-old", scope="agent:summaries", age_days=20, text=text))
        store.put(summary("summary-expired", scope="agent:summaries", age_days=95, text=text, evidence=[ref, CAPTURE]))
        for index, confidence in enumerate((0.4, 0.5, 0.6, 0.7, 0.8)):
            ev = [ref, CAPTURE] if index == 0 else ([ref] if index == 4 else [])
            store.put(record(f"cap-{index}", scope="agent:capacity", confidence=confidence, evidence=ev))
        store.put(record("stale-insight", scope="agent:insights", rtype="meta_insight", confidence=1.0, age_days=70,
                         evidence=[ref, CAPTURE]))
        seeded["edges_after_seed"] = edges(artifactctl, art.sha256)
        stores.append(store)
        return resolved

    def recording(name: str, helper: Any) -> Any:
        def call(store: Any, **kwargs: Any) -> Any:
            caller = sys._getframe(1).f_code.co_name
            try:
                result = helper(store, **kwargs)
            except Exception as exc:  # noqa: BLE001
                calls.append((name, type(store).__name__, f"raised {type(exc).__name__}: {exc} (caller {caller})"))
                raise
            calls.append((name, type(store).__name__, "ok"))
            return result

        return call

    class Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__(logging.WARNING)
            self.seen: set[int] = set()

        def emit(self, rec: logging.LogRecord) -> None:
            if id(rec) not in self.seen:
                self.seen.add(id(rec))
                warnings.append(f"{rec.name} {rec.levelname}: {rec.getMessage()}")

    capture = Capture()
    loggers = (logging.getLogger(), logging.getLogger("openminion"))
    runtime_memory.resolve_memory_backend = seeded_backend
    for name, original in originals.items():
        setattr(session_lifecycle, name, recording(name, original))
    for lg in loggers:
        lg.addHandler(capture)
    row: dict[str, Any] = {}
    try:
        path = echo_config(root)
        manager = ConfigManager.load(str(path), home_root=root, data_root=root / ".openminion")

        def memory_config(**kwargs: Any) -> Any:
            memory = from_base_config(**kwargs)
            memory = dataclasses.replace(memory, retention=dataclasses.replace(memory.retention, max_records_per_scope=3))
            if pg is None:
                return memory
            return dataclasses.replace(memory, store=dataclasses.replace(memory.store, backend="postgres",
                                                                          postgres_url=pg.url))

        manager.register("memory", memory_config)
        runtime = APIRuntime.from_manager(manager)
        try:
            run_states = []
            with Agent(runtime=runtime) as agent:
                run_states.append(agent.run("hello", session_id="probe392-session").run_state)
                calls_after_first = list(calls)
                warnings_after_first = list(warnings)
                store = stores[0]
                store.put(record("stale-insight-2", scope="agent:insights", rtype="meta_insight", confidence=1.0,
                                 age_days=70, evidence=[seeded["ref"], CAPTURE]))
                run_states.append(agent.run("hello again", session_id="probe392-session").run_state)
            [assembly] = list(runtime._memory_assemblies.values())
            service_store = assembly.service._store
            ids = ["decays", "decay-evicted", "summary-old", "summary-expired", *(f"cap-{i}" for i in range(5)),
                   "stale-insight", "stale-insight-2"]
            row = {
                "run_states": run_states,
                "store_passed_to_helpers": f"{type(service_store).__name__} wrapping "
                                           f"{type(getattr(service_store, '_store', None)).__name__}",
                "seeded_store_is_wrapped_store": getattr(service_store, "_store", None) is store,
                "helper_calls": [list(x) for x in calls],
                "helper_call_counts": {f"{n} | {t} | {r}": k for (n, t, r), k in collections.Counter(calls).items()},
                "second_turn_added_helper_calls": len(calls) - len(calls_after_first),
                "warnings": warnings,
                "warnings_after_first_turn": len(warnings_after_first),
                "decays_confidence": round(float(store.get("decays").confidence), 3),
                "summary_old_text": store.get("summary-old").content["summary_text"],
                "deleted_reasons": {rid: deleted_reason(store, rid) for rid in ids},
                "evidence": {rid: evidence_of(store, rid) for rid in
                             ("decays", "decay-evicted", "summary-expired", "cap-0", "cap-4", "stale-insight",
                              "stale-insight-2")},
                "artifact_edges_after_seed": seeded["edges_after_seed"],
                "artifact_edges_after_turns": edges(seeded["artifactctl"], seeded["sha"]),
            }
            if pg is not None:
                server_all = {
                    r[0]: {"confidence": round(float(r[1]), 3), "is_deleted": bool(r[2]), "deleted_reason": r[3],
                           "evidence_jsonb_type": r[4], "evidence_items": r[5], "type": r[6], "scope": r[7]}
                    for r in pg.rows("SELECT id, confidence, is_deleted, deleted_reason, jsonb_typeof(evidence_json), "
                                     "jsonb_array_length(evidence_json), type, scope FROM memory_records ORDER BY id")
                }
                # The turns also write records of their own (not seeded); they are listed separately.
                row["server_rows"] = {k: v for k, v in server_all.items() if k in ids}
                row["server_rows_written_by_the_turns"] = {k: v for k, v in server_all.items() if k not in ids}
                row["server_schema"] = "probe392_sdk_<random>"
        finally:
            runtime.close()
    except Exception as exc:  # noqa: BLE001
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        runtime_memory.resolve_memory_backend = resolve_backend
        for name, original in originals.items():
            setattr(session_lifecycle, name, original)
        for lg in loggers:
            lg.removeHandler(capture)
        if pg is not None:
            pg.drop()
    observed[label] = row
    if "error" in row:
        c.check(f"{label}: runtime built and both turns ran", False, row["error"])
        return
    inner = "PostgresMemoryStore" if backend == "postgres" else "SQLiteMemoryStore"
    c.check(f"{label}: both turns completed", row["run_states"] == ["completed", "completed"], row["run_states"])
    c.check(f"{label}: the lifecycle helpers receive the runtime's AuditedMemoryStore wrapping {inner}",
            row["store_passed_to_helpers"] == f"AuditedMemoryStore wrapping {inner}" and
            row["seeded_store_is_wrapped_store"], row["store_passed_to_helpers"])
    expected_counts = {
        "apply_confidence_decay | AuditedMemoryStore | ok": 1,
        "compress_old_summaries_gc | AuditedMemoryStore | ok": 2,
        "enforce_scope_capacity | AuditedMemoryStore | ok": 1,
        "evict_stale_insights | AuditedMemoryStore | ok": 1,
    }
    c.check(f"{label}: every helper call returned (decay, capacity, insights once; summary compression for the "
            f"first-turn preamble and the lifecycle), none raised", row["helper_call_counts"] == expected_counts,
            row["helper_call_counts"], expected_counts)
    c.check(f"{label}: the second turn of the session runs no lifecycle helper (work is not reapplied)",
            row["second_turn_added_helper_calls"] == 0, row["second_turn_added_helper_calls"], 0)
    lifecycle_warnings = [w for w in row["warnings"] if "memory.session_lifecycle" in w]
    connect_warnings = [w for w in row["warnings"] if "_connect" in w]
    c.check(f"{label}: no `memory.session_lifecycle failed` warning and no `_connect` text in any WARNING+ log record",
            not lifecycle_warnings and not connect_warnings, {"lifecycle": lifecycle_warnings,
                                                              "_connect": connect_warnings,
                                                              "all_warnings": len(row["warnings"])})
    c.check(f"{label}: confidence decay: `decays` 0.8 -> 0.6 (28 days at 0.05 per 7 days)",
            row["decays_confidence"] == 0.6, row["decays_confidence"], 0.6)
    c.check(f"{label}: summary compression: `summary-old` text compressed to its first sentence",
            row["summary_old_text"] == "Decided to use pytest", row["summary_old_text"], "Decided to use pytest")
    expected_reasons = {
        "decays": None, "decay-evicted": "confidence_below_threshold",
        "summary-old": None, "summary-expired": "summary_retention",
        "cap-0": "scope_capacity", "cap-1": "scope_capacity", "cap-2": None, "cap-3": None, "cap-4": None,
        "stale-insight": "stale_insight", "stale-insight-2": None,
    }
    c.check(f"{label}: the intended records are soft-deleted with their reason (decay eviction, summary deletion, "
            f"capacity, stale insight) and the others stay active; `stale-insight-2`, added after turn 1, stays",
            row["deleted_reasons"] == expected_reasons, row["deleted_reasons"], expected_reasons)
    ref = seeded.get("ref")
    ref_dict = dict(sorted(vars(ref).items())) if ref is not None else None
    cap_dict = dict(sorted(vars(CAPTURE).items()))
    expected_evidence = {"decays": [ref_dict], "decay-evicted": [ref_dict, cap_dict],
                         "summary-expired": [ref_dict, cap_dict], "cap-0": [ref_dict, cap_dict], "cap-4": [ref_dict],
                         "stale-insight": [ref_dict, cap_dict], "stale-insight-2": [ref_dict, cap_dict]}
    c.check(f"{label}: evidence lists (artifact ref and capture evidence) read back unchanged after the lifecycle, "
            f"on removed and kept records", row["evidence"] == expected_evidence, row["evidence"])
    expected_edges = {"cap-0": "removed", "cap-4": "active", "decay-evicted": "removed", "decays": "active",
                      "stale-insight": "removed", "stale-insight-2": "active", "summary-expired": "removed"}
    c.check(f"{label}: artifact index: the edges of the removed records are removed, the edges of kept records stay "
            f"(capture evidence makes no edge)",
            row["artifact_edges_after_seed"] == {k: "active" for k in expected_edges if k != "stale-insight-2"}
            and row["artifact_edges_after_turns"] == expected_edges,
            {"after_seed": row["artifact_edges_after_seed"], "after_turns": row["artifact_edges_after_turns"]},
            expected_edges)
    if backend == "postgres":
        server = row["server_rows"]
        server_reasons = {k: (v["deleted_reason"] if v["is_deleted"] else None) for k, v in server.items()}
        c.check(f"{label}: the same outcome for the seeded records read server-side from the PostgreSQL schema "
                f"(memory_records)",
                server_reasons == expected_reasons and server["decays"]["confidence"] == 0.6,
                {"reasons": server_reasons, "decays": server["decays"]["confidence"],
                 "other_rows_written_by_the_turns": {k: [v["type"], v["scope"], v["deleted_reason"]]
                                                     for k, v in row["server_rows_written_by_the_turns"].items()}},
                expected_reasons)
        c.check(f"{label}: evidence_json is a JSONB array on the server for the records with evidence",
                all(server[k]["evidence_jsonb_type"] == "array" and server[k]["evidence_items"] == len(v)
                    for k, v in expected_evidence.items()),
                {k: (server[k]["evidence_jsonb_type"], server[k]["evidence_items"]) for k in expected_evidence})


# ---------------------------------------------------------------- B. helpers, direct and audited
def scenario_decay(target: Any, store: Any) -> tuple[Any, Any]:
    store.put(record("decays", confidence=0.8, age_days=28))
    store.put(record("evicted", confidence=0.35, age_days=28))
    store.put(record("recent", confidence=0.8, age_days=1))
    store.put(record("pinned", rtype="pin", confidence=0.8, age_days=28))
    store.put(record("in-session", scope="session:s1", confidence=0.8, age_days=28))
    result = apply_confidence_decay(target, interval_days=7, decay_rate=0.05, min_confidence=0.3)
    ids = ("decays", "evicted", "recent", "pinned", "in-session")
    got = (list(result), {i: [round(float(store.get(i).confidence), 3), deleted_reason(store, i)] for i in ids})
    want = ([2, 1], {"decays": [0.6, None], "evicted": [0.15, "confidence_below_threshold"], "recent": [0.8, None],
                     "pinned": [0.8, None], "in-session": [0.8, None]})
    return got, want


def scenario_summaries(target: Any, store: Any) -> tuple[Any, Any]:
    store.put(summary("summary-old", age_days=20, text="Decided to use pytest. Actually, wrong fixture scope."))
    store.put(summary("summary-expired", age_days=100, text="Old session."))
    store.put(summary("summary-recent", age_days=2, text="Recent session. Keep every sentence."))
    result = compress_old_summaries(target, max_age_days=14, delete_age_days=90, max_summary_chars=100)
    old = store.get("summary-old").content
    got = (list(result), old["summary_text"], old["decisions"], store.get("summary-recent").content["summary_text"],
           {i: deleted_reason(store, i) for i in ("summary-old", "summary-expired", "summary-recent")})
    want = ([1, 1], "Decided to use pytest", ["decided to use pytest"], "Recent session. Keep every sentence.",
            {"summary-old": None, "summary-expired": "summary_retention", "summary-recent": None})
    return got, want


def scenario_capacity(target: Any, store: Any) -> tuple[Any, Any]:
    for index, confidence in enumerate((0.4, 0.5, 0.6, 0.7, 0.8)):
        store.put(record(f"fact-{index}", scope="agent:capacity", confidence=confidence))
    store.put(record("pin", scope="agent:capacity", rtype="pin", confidence=0.1))
    store.put(record("other-1", scope="agent:other", confidence=0.1))
    store.put(record("other-2", scope="agent:other", confidence=0.1))
    result = enforce_scope_capacity(target, max_records=4)
    ids = ("fact-0", "fact-1", "fact-2", "fact-3", "fact-4", "pin", "other-1", "other-2")
    got = (dict(result), {i: deleted_reason(store, i) for i in ids})
    want = ({"agent:capacity": 2}, {"fact-0": "scope_capacity", "fact-1": "scope_capacity", "fact-2": None,
                                    "fact-3": None, "fact-4": None, "pin": None, "other-1": None, "other-2": None})
    return got, want


def scenario_insights(target: Any, store: Any) -> tuple[Any, Any]:
    store.put(record("stale", rtype="meta_insight", age_days=100))
    store.put(record("old-but-used", rtype="meta_insight", age_days=100, last_hit_days_ago=5))
    store.put(record("fresh", rtype="meta_insight", age_days=2))
    store.put(record("old-fact", rtype="fact", age_days=100))
    result = evict_stale_insights(target, staleness_days=60)
    got = (result, {i: deleted_reason(store, i) for i in ("stale", "old-but-used", "fresh", "old-fact")})
    want = (1, {"stale": "stale_insight", "old-but-used": None, "fresh": None, "old-fact": None})
    return got, want


SCENARIOS = {"confidence decay": scenario_decay, "summary compression/deletion": scenario_summaries,
             "scope capacity": scenario_capacity, "stale insight eviction": scenario_insights}


def helper_scenarios(backend: str) -> None:
    for sname, scenario in SCENARIOS.items():
        for kind in ("direct", "audited"):
            label = f"B.{backend}.{kind}: {sname}"
            pg = PgSchema("gc") if backend == "postgres" else None
            engine = None
            try:
                if pg is None:
                    store = SQLiteMemoryStore(WORK / f"gc-{uuid.uuid4().hex[:8]}" / "memory.db")
                else:
                    from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore

                    engine = sa.create_engine(pg.url, future=True)
                    store = PostgresMemoryStore(engine, database_path=WORK / f"pg-{uuid.uuid4().hex[:8]}" / "memory.db")
                sink = InMemoryMemoryAuditSink()
                target = store if kind == "direct" else AuditedMemoryStore(store, sink=sink)
                try:
                    got, want = scenario(target, store)
                    got, want = json.loads(json.dumps(got)), json.loads(json.dumps(want))
                    observed[label] = {"target": f"{type(target).__name__}({type(store).__name__})" if kind == "audited"
                                       else type(store).__name__, "observed": got, "audit_events": len(sink.events)}
                    c.check(f"{label}: intended records changed, others untouched, no audit events",
                            got == want and sink.events == [], {"observed": got, "audit_events": len(sink.events)}, want)
                except Exception as exc:  # noqa: BLE001
                    observed[label] = {"error": f"{type(exc).__name__}: {exc}"}
                    c.check(f"{label}: intended records changed, others untouched, no audit events", False,
                            f"{type(exc).__name__}: {exc}")
                finally:
                    store.close()
            finally:
                if engine is not None:
                    engine.dispose()
                if pg is not None:
                    pg.drop()


backends = ["sqlite"] + (["postgres"] if PG_BASE else [])
for be in backends:
    sdk_lifecycle(be)
for be in backends:
    helper_scenarios(be)
if admin is not None:
    admin.dispose()

summary_counts = c.summary()
write_result(OUT, {"info": info, "backends": backends, "observed": observed, "checks": c.items, "summary": summary_counts})
print(f"SUMMARY passed={summary_counts['passed']} failed={summary_counts['failed']} total={summary_counts['total']} "
      f"backends={backends}")
sys.exit(0 if c.all_pass else 1)
