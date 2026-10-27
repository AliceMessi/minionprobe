"""#401 on the installed package: `memctl gc` purges through the audited store
memctl builds, on PostgreSQL, with SQLite controls.

    python -I -X utf8 probe_401.py <work_dir> <out.json> [--postgres]

Backends: SQLite always; PostgreSQL with --postgres (OPENMINION_TEST_POSTGRES_URL,
each case in a new schema). Every case starts from a fresh store seeded by the
installed package (probe_401_lib.seed) in agent:a, with real artifacts in the
case's default artifact store as evidence:
  old      fact 0.8, 28 days old, evidence    -> retention decays it to 0.6; kept
  low      fact 0.35, 28 days old, evidence   -> decays below 0.3: soft-deleted, then purged
  deleted  fact 0.8, evidence, soft-deleted   -> purged
  fresh    fact 0.9                           -> untouched
  pref-v1  keyed fact, then upserted          -> v1 superseded (soft-deleted) but protected; kept
  candidates proposed, approved (kept), rejected (with evidence) and promoted (purged)

1. The real `memctl gc --json` (installed console script, own process) with a
   typed memctl config file (MEMCTL_CONFIG, no --db) selecting the store, so
   memctl builds its own AuditedMemoryStore and runs the retention pass and the
   purge. Rows, candidates and evidence are read server-side before and after,
   the artifact reference edges from the artifact index, and the audit database.
   In process, memctl's `_get_service()` under the same environment shows the
   wrapper it builds.
2. run_gc in process with memctl's retention config on the bare store and on
   AuditedMemoryStore(store) (in-memory audit sink): the same outcome as the CLI.
3. purge_soft_deleted alone, bare and audited: only the soft-deleted record and
   the rejected/promoted candidates go.
4. run_gc's retention pass (decay, summary compression, scope capacity, same
   arguments) without purge_soft_deleted, through the audited store:
   soft-deleted rows stay (the hard purge happens only in the explicit purge).
5. Error behavior, bare, audited and the CLI: a BEFORE DELETE trigger on
   memory_records makes the purge fail; the same exception from the bare and
   the audited store, the purge transaction rolled back. SQLite only: stored
   evidence_json that is not JSON still raises JSONDecodeError. `memctl gc` on a
   store without SQL support (store.backend: mock) keeps its CLI error.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize_text, write_result  # noqa: E402
from probe_401_lib import (  # noqa: E402
    PG_BASE,
    Artifacts,
    Case,
    PgBackend,
    SqliteBackend,
    environ,
    redact,
    seed,
)

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
POSTGRES = "--postgres" in sys.argv[3:]
quiet()
clear_override_env()

extra: dict[str, Any] = {"postgres_requested": POSTGRES}
admin = None
if POSTGRES:
    if not PG_BASE:
        extra["postgres"] = "NOT RUN: OPENMINION_TEST_POSTGRES_URL is not set"
    else:
        import psycopg
        import sqlalchemy as sa

        admin = sa.create_engine(PG_BASE, future=True)
        with admin.connect() as _conn:
            extra["postgres_version"] = _conn.execute(sa.text("SELECT version()")).scalar_one()
            extra["postgres_server_version"] = _conn.execute(sa.text("SHOW server_version")).scalar_one()
        extra["sqlalchemy"] = sa.__version__
        extra["psycopg"] = psycopg.__version__
info = banner("probe_401", **extra)

from openminion.modules.memory import cli as memory_cli  # noqa: E402
from openminion.modules.memory.config import load_config  # noqa: E402
from openminion.modules.memory.runtime.gc import (  # noqa: E402
    apply_confidence_decay,
    compress_old_summaries,
    enforce_scope_capacity,
    run_gc,
)
from openminion.modules.memory.runtime.purge import purge_soft_deleted  # noqa: E402
from openminion.modules.memory.storage.audit import AuditedMemoryStore, InMemoryMemoryAuditSink  # noqa: E402

c = Checks()
observed: dict[str, Any] = {}

EXPECTED_RESULT = {"decayed_records": 2, "capacity_evicted_records": 0, "compressed_summaries": 0, "deleted_records": 2,
                   "deleted_candidates": 2}
KEPT_CANDIDATES = {"c-approved": "approved", "c-proposed": "proposed"}
ALL_CANDIDATES = {"c-approved": "approved", "c-promoted": "promoted", "c-proposed": "proposed", "c-rejected": "rejected"}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
GC_RESULT_KEYS = ("decayed_records", "capacity_evicted_records", "compressed_summaries", "deleted_records",
                  "deleted_candidates")


def memctl_exe() -> Path:
    return Path(sys.executable).with_name("memctl.exe" if os.name == "nt" else "memctl")


def run_memctl(case: Case, *args: str) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENMINION_", "MEMCTL_"))}
    env.update(case.env, PYTHONUTF8="1")
    proc = subprocess.run([str(memctl_exe()), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=str(case.root), timeout=900)
    lines = proc.stdout.splitlines()
    result = None
    if lines:
        try:
            parsed = json.loads(lines[0])
            result = {k: parsed.get(k) for k in GC_RESULT_KEYS} if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            result = None
    err = [redact(sanitize_text(ANSI.sub("", x))).strip() for x in proc.stderr.strip().splitlines() if x.strip()]
    return {"exit_code": proc.returncode, "result": result,
            "GC line": next((ln for ln in lines if ln.startswith("GC:")), None),
            "stderr_tail": err[-2:], "stderr lines naming the trigger's error": [x[:240] for x in err if "probe: purge blocked" in x][:2],
            "traceback": "Traceback" in proc.stderr}


def retention_config(case: Case) -> Any:
    with environ(case.env):
        return load_config(env=dict(os.environ)).retention


def retention_steps(target: Any, retention: Any) -> None:
    """run_gc's retention pass (same arguments), without purge_soft_deleted."""
    apply_confidence_decay(target, interval_days=int(getattr(retention, "confidence_decay_interval_days", 7)),
                           decay_rate=float(getattr(retention, "confidence_decay_rate", 0.05)),
                           min_confidence=float(getattr(retention, "min_confidence_eviction", 0.3)),
                           disuse_threshold_days=int(getattr(retention, "disuse_threshold_days", 30)),
                           disuse_decay_multiplier=float(getattr(retention, "disuse_decay_multiplier", 2.0)))
    compress_old_summaries(target, max_age_days=int(retention.summary_compression_age_days),
                           delete_age_days=int(retention.summary_delete_age_days),
                           max_summary_chars=int(retention.summary_compression_max_chars))
    enforce_scope_capacity(target, max_records=int(getattr(retention, "max_records_per_scope", 500)))


def describe_exc(exc: BaseException) -> str:
    first = (str(exc).strip().splitlines() or [""])[0]
    return redact(sanitize_text(f"{type(exc).__module__}.{type(exc).__name__}: {first}"))


def refs_as_dicts(refs: list) -> list[dict]:
    return [{k: getattr(r, k) for k in ("ref", "sha256", "mime", "size_bytes")} for r in refs]


class Fresh:
    """A new case: environment, backend, artifact store and seeded store."""

    def __init__(self, backend_name: str, label: str) -> None:
        root = WORK / "run" / f"{backend_name}-{label}"
        self.backend = SqliteBackend(root) if backend_name == "sqlite" else PgBackend(root, admin)
        self.case = Case(root, self.backend)
        self.artifacts = Artifacts(self.case)
        self.store = self.backend.store(self.artifacts.ctl)
        self.refs = seed(self.store, self.artifacts)
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.store.close()
            self.artifacts.close()

    def finish(self) -> None:
        self.close()
        self.backend.drop()


def target_for(kind: str, store: Any) -> tuple[Any, Any]:
    if kind == "bare":
        return store, None
    sink = InMemoryMemoryAuditSink()
    return AuditedMemoryStore(store, sink=sink), sink


backends = ["sqlite"] + (["postgres"] if admin is not None else [])
if (WORK / "run").exists():
    shutil.rmtree(WORK / "run")
seeded: dict[str, Any] = {}
for b in backends:
    res: dict[str, Any] = {}

    # Seed state (shared expectation base).
    f = Fresh(b, "seed")
    seeded[b] = {"state": f.backend.state(), "edges": f.artifacts.edges(),
                 "artifact index under the case root": f.artifacts.under_case_root}
    f.finish()
    res["seeded state (server-side)"] = seeded[b]
    s0 = seeded[b]["state"]
    v1 = s0["records"].get("pref-v1")
    keyed_new = next((k for k in s0["records"] if k.endswith("(upserted)")), None)
    c.check(f"{b}: seed has 6 rows and 4 candidates; pref-v1 superseded (soft-deleted) by an active upserted version; "
            "artifact edges for the evidence",
            s0["memory_records rows"] == 6 and s0["memory_candidates rows"] == 4 and isinstance(v1, list)
            and v1[1] != "active" and keyed_new is not None and s0["records"][keyed_new][1] == "active"
            and seeded[b]["artifact index under the case root"]
            and all(seeded[b]["edges"].get(f"memory:{o}") == "active" for o in ("old", "low", "c-rejected")),
            seeded[b])
    kept_v1 = {"pref-v1": v1, keyed_new: [0.9, "active"]} if keyed_new else {}
    expect_gc = {"records": dict(sorted({"old": [0.6, "active"], "fresh": [0.9, "active"], **kept_v1}.items())),
                 "candidates": KEPT_CANDIDATES}
    expect_purge_only = {"records": dict(sorted({"old": [0.8, "active"], "low": [0.35, "active"], "fresh": [0.9, "active"],
                                                 **kept_v1}.items())),
                         "candidates": KEPT_CANDIDATES}
    expect_retention_only = {"records": dict(sorted({"old": [0.6, "active"], "low": [0.15, "confidence_below_threshold"],
                                                     "deleted": s0["records"].get("deleted"), "fresh": [0.9, "active"],
                                                     **kept_v1}.items())),
                             "candidates": ALL_CANDIDATES}
    edges_after_gc = {**seeded[b]["edges"], "memory:old": "active", "memory:low": "removed", "memory:deleted": "removed",
                      "memory:c-rejected": "removed"}

    def core(state: dict) -> dict:
        return {"records": state["records"], "candidates": state["candidates"]}

    # 1. The real memctl gc.
    f = Fresh(b, "memctl-gc")
    before = f.backend.state()
    schema = f.backend.schema_check() if b == "postgres" else None
    f.close()
    cli = run_memctl(f.case, "gc", "--json")
    after = f.backend.state()
    edges = f.artifacts.edges()
    reader = f.backend.store(None)
    old = reader.get("old")
    old_evidence = refs_as_dicts(old.evidence_refs) if old else None
    reader.close()
    with environ(f.case.env):
        svc = memory_cli._get_service(None)
        wrapper = f"{type(svc._store).__name__} wrapping {type(getattr(svc._store, '_store', None)).__name__}"
        svc.close()
    row = {"memctl gc --json": cli, "store memctl builds (_get_service, same environment)": wrapper,
           "before (server-side)": before, "after (server-side)": after, "artifact edges after": edges,
           "old evidence server-side after": f.backend.evidence("old"),
           "old evidence read back": old_evidence, "audit database after": f.case.audit_events()}
    if schema is not None:
        row["schema"] = schema
    res["1. memctl gc (real CLI, typed config)"] = row
    store_name = "SQLiteMemoryStore" if b == "sqlite" else "PostgresMemoryStore"
    c.check(f"{b}: memctl builds AuditedMemoryStore wrapping {store_name} for gc", wrapper == f"AuditedMemoryStore wrapping {store_name}",
            wrapper)
    if b == "postgres":
        c.check("postgres: the backend really ran: the seed (6 rows, 4 candidates) is on the live server in the probe's own "
                "schema, and memctl gc changed it there (old decayed to 0.6, read server-side)",
                (schema or {}).get("current_schema is the probe's own") is True
                and (schema or {}).get("memory_records table in it") == 1
                and before["memory_records rows"] == 6 and before["memory_candidates rows"] == 4
                and before["records"].get("old") == [0.8, "active"] and after["records"].get("old") == [0.6, "active"],
                {"schema": schema, "rows before": before["memory_records rows"], "rows after": after["memory_records rows"],
                 "candidates before": before["memory_candidates rows"], "candidates after": after["memory_candidates rows"],
                 "server": extra.get("postgres_server_version")})
    c.check(f"{b}: memctl gc exits 0 and reports deleted_records=2, deleted_candidates=2 (retention: 2 decayed)",
            cli["exit_code"] == 0 and cli["GC line"] == "GC: deleted_records=2, deleted_candidates=2"
            and cli["result"] == EXPECTED_RESULT, cli, EXPECTED_RESULT)
    c.check(f"{b}: after memctl gc the soft-deleted records and the rejected/promoted candidates are gone; old decayed to 0.6, "
            "fresh and the superseded keyed history kept (server-side: 6 -> 4 rows, 4 -> 2 candidates)",
            core(after) == expect_gc and before["memory_records rows"] == 6 and after["memory_records rows"] == 4
            and before["memory_candidates rows"] == 4 and after["memory_candidates rows"] == 2,
            {**core(after), "rows before/after": [before["memory_records rows"], after["memory_records rows"]],
             "candidates before/after": [before["memory_candidates rows"], after["memory_candidates rows"]]}, expect_gc)
    c.check(f"{b}: evidence: retained old keeps its evidence (server-side and read back as written), its artifact edge stays; "
            "edges of purged low, deleted and c-rejected removed",
            row["old evidence server-side after"] == (["text", 1] if b == "sqlite" else ["array", 1])
            and old_evidence == refs_as_dicts([f.refs["old"]]) and edges == edges_after_gc,
            {k: row[k] for k in ("old evidence server-side after", "artifact edges after")}, {"edges": edges_after_gc})
    audit = row["audit database after"]
    c.check(f"{b}: memctl gc wrote no audit events (retention and purge writes are not audited)",
            audit == "no audit database" or (isinstance(audit, dict) and all(v == [] for v in audit.values())), audit)
    f.backend.drop()

    # 2. run_gc in process: bare vs audited.
    for kind in ("bare", "audited"):
        f = Fresh(b, f"run_gc-{kind}")
        target, sink = target_for(kind, f.store)
        try:
            r = run_gc(target, batch_size=500, retention_config=retention_config(f.case))
            outcome: Any = {k: getattr(r, k) for k in GC_RESULT_KEYS}
        except Exception as exc:  # noqa: BLE001
            outcome = f"raised {describe_exc(exc)}"
        state = f.backend.state()
        row = {"run_gc result": outcome, "after (server-side)": core(state), "artifact edges after": f.artifacts.edges(),
               "audit events": None if sink is None else len(sink.events)}
        res[f"2. run_gc in process on the {kind} store"] = row
        f.finish()
        c.check(f"{b}: run_gc on the {kind} {'store' if kind == 'bare' else 'AuditedMemoryStore'}: the expected result, rows, "
                "candidates and edges (those memctl gc must give)" + ("; no audit events" if sink is not None else ""),
                outcome == EXPECTED_RESULT and row["after (server-side)"] == expect_gc
                and row["artifact edges after"] == edges_after_gc and row["audit events"] in (None, 0), row)

    # 3. purge_soft_deleted alone: bare vs audited.
    for kind in ("bare", "audited"):
        f = Fresh(b, f"purge-{kind}")
        target, sink = target_for(kind, f.store)
        try:
            r = purge_soft_deleted(target)
            outcome = [r.deleted_records, r.deleted_candidates]
        except Exception as exc:  # noqa: BLE001
            outcome = f"raised {describe_exc(exc)}"
        row = {"purge_soft_deleted result (records, candidates)": outcome, "after (server-side)": core(f.backend.state()),
               "audit events": None if sink is None else len(sink.events)}
        res[f"3. purge_soft_deleted on the {kind} store"] = row
        f.finish()
        c.check(f"{b}: purge_soft_deleted on the {kind} store: (1, 2); only the soft-deleted record and the rejected/promoted "
                "candidates go", outcome == [1, 2] and row["after (server-side)"] == expect_purge_only
                and row["audit events"] in (None, 0), row, expect_purge_only)

    # 4. Retention helpers without the purge, through the audited store.
    f = Fresh(b, "retention-only")
    target, sink = target_for("audited", f.store)
    try:
        retention_steps(target, retention_config(f.case))
        outcome = "ok"
    except Exception as exc:  # noqa: BLE001
        outcome = f"raised {describe_exc(exc)}"
    state = f.backend.state()
    row = {"retention helpers": outcome, "after (server-side)": core(state), "memory_records rows": state["memory_records rows"],
           "audit events": len(sink.events)}
    res["4. run_gc's retention pass without purge_soft_deleted (audited store)"] = row
    f.finish()
    c.check(f"{b}: run_gc's retention pass alone (audited) soft-deletes low and leaves every soft-deleted row in place "
            "(6 rows); the hard purge is only memctl gc / purge_soft_deleted",
            outcome == "ok" and row["after (server-side)"] == expect_retention_only and row["memory_records rows"] == 6,
            row, expect_retention_only)

    # 5. Error behavior: a failing purge statement.
    raised: dict[str, str] = {}
    for kind in ("bare", "audited"):
        f = Fresh(b, f"blocked-{kind}")
        f.backend.block_purge()
        before = core(f.backend.state())
        target, sink = target_for(kind, f.store)
        try:
            r = purge_soft_deleted(target)
            outcome = f"returned ({r.deleted_records}, {r.deleted_candidates})"
        except Exception as exc:  # noqa: BLE001
            outcome = f"raised {describe_exc(exc)}"
        raised[kind] = outcome
        after = core(f.backend.state())
        row = {"purge_soft_deleted": outcome, "rows and candidates unchanged (rolled back)": after == before}
        res[f"5. purge blocked by a trigger, {kind} store"] = row
        f.finish()
        c.check(f"{b}: purge failing inside its transaction ({kind} store): raises the trigger's error and rolls back",
                "probe: purge blocked" in outcome and row["rows and candidates unchanged (rolled back)"], row)
    c.check(f"{b}: the audited store raises the same error as the bare store (exception behavior unchanged)",
            raised["bare"] == raised["audited"] and "probe: purge blocked" in raised["bare"], raised)
    f = Fresh(b, "blocked-memctl")
    f.close()
    f.backend.block_purge()
    cli = run_memctl(f.case, "gc")
    after = core(f.backend.state())
    res["5. memctl gc with the purge blocked"] = {"memctl gc": cli, "after (server-side)": after}
    f.backend.drop()
    c.check(f"{b}: memctl gc with the purge blocked: exit 1, no GC line, the trigger's error on stderr; purge rolled back "
            "(soft-deleted rows and all candidates still there), the retention pass before it committed",
            cli["exit_code"] == 1 and cli["GC line"] is None and len(cli["stderr lines naming the trigger's error"]) > 0
            and after == expect_retention_only, {"memctl gc": cli, "after": after}, expect_retention_only)

    if b == "sqlite":
        for kind in ("bare", "audited"):
            f = Fresh(b, f"badjson-{kind}")
            f.backend.corrupt_evidence("deleted")
            before = core(f.backend.state())
            target, _sink = target_for(kind, f.store)
            try:
                r = purge_soft_deleted(target)
                outcome = f"returned ({r.deleted_records}, {r.deleted_candidates})"
            except Exception as exc:  # noqa: BLE001
                outcome = f"raised {describe_exc(exc)}"
            row = {"purge_soft_deleted": outcome, "rows unchanged": core(f.backend.state()) == before}
            res[f"5. stored evidence_json 'not json', {kind} store"] = row
            f.finish()
            c.check(f"sqlite: a soft-deleted record whose stored evidence_json is not JSON: purge_soft_deleted on the {kind} "
                    "store still raises JSONDecodeError and rolls back",
                    outcome.startswith("raised json.decoder.JSONDecodeError") and row["rows unchanged"], row)
        root = WORK / "run" / "mock-memctl"
        mock_case = Case(root, SqliteBackend(root))
        mock_case.write_config({"backend": "mock"})
        cli = run_memctl(mock_case, "gc")
        res["5. memctl gc with store.backend: mock (no SQL store)"] = cli
        c.check("memctl gc on a store without SQL support (store.backend: mock) keeps its CLI error: exit 1, "
                "'Error: gc is not supported for this memory store (...)', no traceback",
                cli["exit_code"] == 1 and len(cli["stderr_tail"]) == 1
                and cli["stderr_tail"][0].startswith("Error: gc is not supported for this memory store (")
                and not cli["traceback"], cli)

    observed[b] = res

if POSTGRES and admin is None:
    c.check("postgres requested but OPENMINION_TEST_POSTGRES_URL is not set (a skip is not a pass)", False, "not run")
if admin is not None:
    admin.dispose()
s = c.summary()
write_result(OUT, {"info": info, "backends": backends, "observed": observed, "checks": c.items, "summary": s})
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']} backends={backends}")
sys.exit(0 if c.all_pass else 1)
