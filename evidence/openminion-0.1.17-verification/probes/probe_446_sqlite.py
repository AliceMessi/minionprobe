"""#446 item 4 on SQLite: runtime/SQLite initialization, migrations and ordinary storage unchanged.

    python -I -X utf8 probe_446_sqlite.py WORK OUT.json [--baseline OTHER.json]

On the installed package, with no PostgreSQL:
- runtime storage (build_runtime_storage) on a new SQLite file: v15, a session and a message
  written and read back, the next start applies nothing; every sqlite3 connection it opened is
  closed after RuntimeStorageContext.close() (sqlite3.connect is wrapped and every connection
  kept, so "closed" is checked directly);
- rollback on an initialization failure: a file holding another application's `event_run_facts`
  table (v13 creates that table): the start fails at v13, v1..v12 stay committed, nothing of v13
  remains, the other table and its row are unchanged, every connection is closed;
- every module's `openminion storage migrate --sqlite <new empty file> --module <id> --json` (the
  installed console script, from an empty directory), and a rerun;
- the SQLite module stores (memory, session, A2A audit and state, registry, ...) open, round-trip
  and close.
Each file's fingerprint (sqlite_master, PRAGMA user_version and application_id, ledgers) is
written to the JSON; with --baseline (the same probe on the published 0.1.16) the fingerprints,
outcomes and connection results must be identical: SQLite behavior is unchanged by #446.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import Checks, SqliteTracker, banner, quiet, write_result  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
RUNS: list[dict] = []


def console() -> str:
    return str(Path(sys.executable).with_name("openminion.exe" if os.name == "nt" else "openminion"))


def cli(argv: list[str], root: Path) -> dict:
    cwd = root / f"cwd-{uuid.uuid4().hex[:6]}"
    cwd.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENMINION_STORAGE")}
    env.update({"OPENMINION_HOME": str(root / "home"), "OPENMINION_DATA_ROOT": str(root / "data")})
    proc = subprocess.run([console(), *argv], cwd=cwd, capture_output=True, text=True, env=env, timeout=600)
    rec = {"argv": ["openminion", *[a if not a.endswith(".db") else Path(a).name for a in argv]],
           "exit": proc.returncode, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-1500:],
           "cwd_left": sorted(p.name for p in cwd.iterdir())}
    RUNS.append(rec)
    return rec


def fingerprint(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        master = sorted(tuple(r) for r in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))
        names = {r[1] for r in master if r[0] == "table"}
        out = {"sqlite_master_sha256": hashlib.sha256(json.dumps(master).encode()).hexdigest(),
               "tables": sorted(names),
               "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
               "application_id": conn.execute("PRAGMA application_id").fetchone()[0]}
        if "alembic_version" in names:
            out["alembic_version"] = sorted(r[0] for r in conn.execute("SELECT version_num FROM alembic_version"))
        if "om_meta" in names:
            out["om_meta"] = {k: v for k, v in conn.execute("SELECT key, value FROM om_meta")
                              if k in ("module_id", "schema_head")}
        if "migrations" in names:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(migrations)")]
            if "name" in cols:
                out["migrations"] = [list(r) for r in conn.execute("SELECT version, name FROM migrations ORDER BY 1")]
        return out
    finally:
        conn.close()


def openminion_connections(tracker: SqliteTracker) -> list[dict]:
    return [s for s in tracker.status() if s["site"] != "(probe code)"]


def runtime_part(c: Checks, work: Path, result: dict) -> None:
    from openminion.modules.storage.runtime.context import build_runtime_storage

    path = work / "runtime" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteTracker(strong=True) as tr:
        ctx = build_runtime_storage(path, env={})
        try:
            first = [ctx.migration_result.current_version, len(ctx.migration_result.applied_versions)]
            s = ctx.sessions.resolve_session(agent_id="a", channel="cli", target="probe")
            ctx.sessions.append_message(session_id=s.id, role="user", body="hello 446")
            msgs = [m.body for m in ctx.sessions.list_messages(session_id=s.id)]
        finally:
            ctx.close()
        conns1 = openminion_connections(tr)
    tr.drop()
    with SqliteTracker(strong=True) as tr:
        ctx = build_runtime_storage(path, env={})
        try:
            second = [ctx.migration_result.current_version, len(ctx.migration_result.applied_versions)]
            again = [m.body for m in ctx.sessions.list_messages(session_id=s.id)]
        finally:
            ctx.close()
        conns2 = openminion_connections(tr)
    tr.drop()
    fp = fingerprint(path)
    result["runtime"] = {"first": first, "second": second, "fingerprint": fp,
                         "connections": [len(conns1), len(conns2)]}
    c.check("item4 SQLite runtime storage: a new file starts at v15 (15 applied), a session and a message are "
            "written and read; the next start applies nothing and reads them back",
            first == [15, 15] and second == [15, 0] and msgs == ["hello 446"] == again,
            {"first": first, "second": second, "messages": [msgs, again]})
    c.check("item4 SQLite runtime storage: migrations holds v1..v15", [r[0] for r in fp.get("migrations", [])]
            == list(range(1, 16)), fp.get("migrations"))
    c.check("item4 lifecycle: every sqlite3 connection runtime storage opened is closed after "
            "RuntimeStorageContext.close(), on both starts", conns1 and conns2 and all(x["closed"] for x in conns1 + conns2),
            {"opened": [len(conns1), len(conns2)], "open": [x["site"] for x in conns1 + conns2 if not x["closed"]]})

    # Rollback on an initialization failure.
    path = work / "init-failure" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE event_run_facts (foreign_id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("INSERT INTO event_run_facts VALUES (7, 'another application')")
    conn.commit()
    conn.close()
    with SqliteTracker(strong=True) as tr:
        try:
            ctx = build_runtime_storage(path, env={})
            ctx.close()
            err = ""
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        conns = openminion_connections(tr)
    tr.drop()
    fp = fingerprint(path)
    raw = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        other = [list(r) for r in raw.execute("SELECT * FROM event_run_facts")]
        other_cols = [r[1] for r in raw.execute("PRAGMA table_info(event_run_facts)")]
    finally:
        raw.close()
    versions = [r[0] for r in fp.get("migrations", [])]
    result["init_failure"] = {"error": err, "versions": versions, "connections": len(conns)}
    c.check("item4 rollback on an initialization failure (SQLite): the start fails with \"Migration v13 "
            "(add_event_run_facts) failed\"; v1..v12 stay committed; the other application's event_run_facts table "
            "and its row are unchanged; every connection the start opened is closed",
            "Migration v13 (add_event_run_facts) failed" in err and versions == list(range(1, 13))
            and other == [[7, "another application"]] and other_cols == ["foreign_id", "note"]
            and conns and all(x["closed"] for x in conns),
            {"error": err[:300], "versions": versions, "open": [x["site"] for x in conns if not x["closed"]]})
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE event_run_facts")
    conn.commit()
    conn.close()
    ctx = build_runtime_storage(path, env={})
    try:
        resumed = [ctx.migration_result.current_version, list(ctx.migration_result.applied_versions)]
    finally:
        ctx.close()
    c.check("item4 rollback (SQLite): once the operator removes that table, the next start applies v13..v15",
            resumed == [15, [13, 14, 15]], resumed)


def migrate_part(c: Checks, work: Path, result: dict) -> None:
    from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS

    modules = sorted(MODULE_APPLICATION_IDS)
    per: dict = {}
    for m in modules:
        path = work / "modules" / f"{m}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()  # `storage migrate --sqlite` migrates an existing file ("Cannot migrate missing DB")
        r1 = cli(["storage", "migrate", "--sqlite", str(path), "--module", m, "--json"], work / "modules")
        fp1 = fingerprint(path) if path.exists() else None
        r2 = cli(["storage", "migrate", "--sqlite", str(path), "--module", m, "--json"], work / "modules")
        fp2 = fingerprint(path) if path.exists() else None
        try:
            entry = json.loads(r1["stdout"])["modules"][0]
        except Exception:  # noqa: BLE001
            entry = {"raw": r1["stdout"][-200:]}
        per[m] = {"exit": [r1["exit"], r2["exit"]], "status": entry.get("status"), "fingerprint": fp1,
                  "rerun_unchanged": fp1 == fp2}
    from openminion.modules.storage.migrations.alembic import discover_module_alembic_paths

    chained = [m for m in modules if discover_module_alembic_paths(m)[1] is not None]
    result["modules"] = per
    result["modules_without_alembic"] = {m: per[m]["exit"] for m in modules if m not in chained}
    print("modules without an Alembic chain (storage migrate reports an error for them):",
          result["modules_without_alembic"])
    c.check("item4 SQLite migrations: `openminion storage migrate --sqlite <new empty file> --module <id> --json` exits 0 "
            f"with status ok for every module with an Alembic chain ({len(chained)} of {len(modules)}), and a rerun "
            "exits 0 and changes nothing",
            all(per[m]["exit"] == [0, 0] and per[m]["status"] == "ok" and per[m]["rerun_unchanged"] for m in chained),
            {m: [per[m]["exit"], per[m]["status"], per[m]["rerun_unchanged"]] for m in chained})
    c.check("item4 SQLite migrations: each of those files records its module (om_meta.module_id) and one Alembic head",
            all((per[m]["fingerprint"] or {}).get("om_meta", {}).get("module_id") == m and len((per[m]["fingerprint"] or {})
                .get("alembic_version", [])) == 1 for m in chained),
            {m: [(per[m]["fingerprint"] or {}).get("alembic_version"), (per[m]["fingerprint"] or {}).get("om_meta")]
             for m in chained})
    c.check("item4 SQLite: no command left a file in its working directory",
            all(not r["cwd_left"] for r in RUNS), [r["argv"] for r in RUNS if r["cwd_left"]][:5])


def stores_part(c: Checks, work: Path, result: dict) -> None:
    import importlib

    from openminion.modules.storage.engine import StorageEngineConfig

    def cfg(root: Path):
        root.mkdir(parents=True, exist_ok=True)
        return StorageEngineConfig(root_dir=root, sqlite_path=root / "state.db", fallback_root=root,
                                   record_backend="record.sqlite", record_backend_options={})

    factories = {
        "session_store": ("openminion.modules.session.runtime.factory", "build_module_session_store", "sessions.db"),
        "a2a_state_store": ("openminion.modules.a2a.storage.factory", "build_a2a_state_store", "a2a.db"),
        "registry_store": ("openminion.modules.registry.storage", "build_registry_store", "registry.db"),
        "artifact_store": ("openminion.modules.artifact.storage", "build_artifact_index", "artifact.db"),
        "identity_store": ("openminion.modules.identity.storage", "build_identity_store", "identity.db"),
        "policy_store": ("openminion.modules.policy.storage", "build_policy_store", "policy.db"),
        "retrieve_store": ("openminion.modules.retrieve.storage", "build_retrieve_store", "retrieve.db"),
        "skill_store": ("openminion.modules.skill.storage", "build_skill_store", "skill.db"),
        "task_store": ("openminion.modules.task.storage", "build_task_store", "task.db"),
        "telemetry_store": ("openminion.modules.telemetry.storage", "build_telemetry_store", "t.db"),
        "controlplane_store": ("openminion.modules.controlplane.storage", "build_controlplane_store", "cp.db"),
        "compress_store": ("openminion.modules.context.compress.storage", "build_compress_telemetry_store",
                           "compress.db"),
    }
    per: dict = {}
    for name, (mod, fn, filename) in factories.items():
        root = work / "stores" / name
        with SqliteTracker(strong=True) as tr:
            try:
                kwargs = {"config": cfg(root), "database_path": root / filename}
                if fn == "build_module_session_store":
                    kwargs["env"] = {}
                store = getattr(importlib.import_module(mod), fn)(**kwargs)
                note = type(store).__name__
                if name == "session_store":
                    sid = store.create_session(initial_agent_id="a", profile_version="v1", title="t", tags=[])
                    note += f" round-trip={store.get_session(sid) is not None}"
                getattr(store, "close", lambda: None)()
                ok = True
            except Exception as exc:  # noqa: BLE001
                ok, note = False, f"{type(exc).__name__}: {str(exc)[:200]}"
            conns = openminion_connections(tr)
        tr.drop()
        db = root / filename
        per[name] = {"ok": ok, "note": note, "fingerprint": fingerprint(db) if db.exists() else None,
                     "connections": [len(conns), sum(1 for x in conns if not x["closed"])]}
    # memory (SQLite) and the A2A audit store (SQLite files under its root)
    from openminion.modules.memory.models import MemoryRecord
    from openminion.modules.memory.storage.factory import resolve_memory_backend

    root = work / "stores" / "memory_store"
    root.mkdir(parents=True, exist_ok=True)
    with SqliteTracker(strong=True) as tr:
        try:
            mem = resolve_memory_backend(config={"store": {"backend": "sqlite"}}, db_path=root / "memory.db").store
            mem.put(MemoryRecord(id="rec-1", scope="agent:a", type="fact", title="t", content="c", created_at=NOW,
                                 updated_at=NOW))
            got = mem.get("rec-1") is not None
            mem.close()
            ok, note = got, f"{type(mem).__name__} round-trip={got}"
        except Exception as exc:  # noqa: BLE001
            ok, note = False, f"{type(exc).__name__}: {str(exc)[:200]}"
        conns = openminion_connections(tr)
    tr.drop()
    per["memory_store"] = {"ok": ok, "note": note, "fingerprint": fingerprint(root / "memory.db")
                           if (root / "memory.db").exists() else None,
                           "connections": [len(conns), sum(1 for x in conns if not x["closed"])]}
    from openminion.modules.a2a.models import AuditRecord
    from openminion.modules.a2a.storage.factory import build_a2a_audit_store

    root = work / "stores" / "a2a_audit_store"
    with SqliteTracker(strong=True) as tr:
        try:
            audit = build_a2a_audit_store(config=cfg(root), audit_root=root / "audit")
            audit.append_audit(AuditRecord(ts=NOW, msg_id="msg-1", trace_id="t", from_agent="a", to_agent="b",
                                           to_capability=None, type="request", method="m", status="ok"))
            got = [r.msg_id for r in audit.query_audit()] == ["msg-1"]
            audit.close()
            ok, note = got, f"{type(audit).__name__} round-trip={got}"
        except Exception as exc:  # noqa: BLE001
            ok, note = False, f"{type(exc).__name__}: {str(exc)[:200]}"
        conns = openminion_connections(tr)
    tr.drop()
    per["a2a_audit_store"] = {"ok": ok, "note": note, "fingerprint": None,
                              "connections": [len(conns), sum(1 for x in conns if not x["closed"])]}
    result["stores"] = per
    c.check("item4 SQLite module stores: every SQLite store factory opens and closes; the memory, session and A2A "
            "audit stores round-trip a row", all(v["ok"] for v in per.values())
            and all("round-trip=True" in v["note"] for k, v in per.items()
                    if k in ("memory_store", "session_store", "a2a_audit_store")),
            {k: v["note"] for k, v in per.items()})
    result["store_connections_left_open"] = {k: v["connections"][1] for k, v in per.items() if v["connections"][1]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("out")
    ap.add_argument("--baseline")
    a = ap.parse_args()
    quiet()
    info = banner("probe_446_sqlite")
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    c = Checks()
    result: dict = {}
    for part in (runtime_part, migrate_part, stores_part):
        try:
            part(c, work, result)
        except Exception as exc:  # noqa: BLE001
            import traceback

            c.check(f"{part.__name__} ran to the end", False, f"{type(exc).__name__}: {exc} {traceback.format_exc()[-800:]}")
    print("store connections left open after close():", result.get("store_connections_left_open"))
    if a.baseline:
        base = json.loads(Path(a.baseline).read_text(encoding="utf-8"))
        b = base.get("result", {})
        c.check(f"item4 SQLite unchanged: the runtime file's fingerprint (sqlite_master, user_version, application_id, "
                f"migrations) is identical to {base['banner']['openminion.__version__']}'s",
                (result.get("runtime") or {}).get("fingerprint") == (b.get("runtime") or {}).get("fingerprint"))
        c.check("item4 SQLite unchanged: the init-failure outcome (error, committed versions, connections) is identical",
                result.get("init_failure") == b.get("init_failure"),
                {"now": result.get("init_failure"), "baseline": b.get("init_failure")})
        mods = {m: (v["exit"], v["status"], v["fingerprint"]) for m, v in (result.get("modules") or {}).items()}
        bmods = {m: (tuple(v["exit"]), v["status"], v["fingerprint"]) for m, v in (b.get("modules") or {}).items()}
        mods = {m: (tuple(x[0]), x[1], x[2]) for m, x in mods.items()}
        c.check("item4 SQLite unchanged: every module's migrated file (sqlite_master, user_version, application_id, "
                "alembic_version, om_meta) and its exit/status are identical",
                mods == bmods, sorted(m for m in set(mods) | set(bmods) if mods.get(m) != bmods.get(m)))
        st = {k: (v["ok"], v["fingerprint"], v["connections"]) for k, v in (result.get("stores") or {}).items()}
        bst = {k: (v["ok"], v["fingerprint"], v["connections"]) for k, v in (b.get("stores") or {}).items()}
        c.check("item4 SQLite unchanged: every store's outcome, file fingerprint and connection counts "
                "(opened, left open) are identical", json.loads(json.dumps(st)) == json.loads(json.dumps(bst)),
                sorted(k for k in set(st) | set(bst) if json.loads(json.dumps(st.get(k))) != json.loads(json.dumps(bst.get(k)))))
    write_result(Path(a.out), {"banner": info, "summary": c.summary(), "checks": c.items, "result": result,
                               "commands": RUNS})
    s = c.summary()
    print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
    return 0 if c.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
