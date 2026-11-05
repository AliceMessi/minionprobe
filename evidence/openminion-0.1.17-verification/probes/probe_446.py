"""#446 on the installed package, PostgreSQL: a schema per owner, unproven layouts refused.

    python -I -X utf8 probe_446.py WORK OUT.json [--legacy-python PY] [--histories JSON]
                                   [--only item1,item2,...]

Runs from an empty directory against OPENMINION_TEST_POSTGRES_URL (a superuser URL), a new
database per case. Items follow the maintainer's checklist (openminion#446 comment 6522959344):

  item1  fresh runtime plus every supported module store/migration on one URL, runtime-first and
         module-first, reopening, colliding table/ledger names, CLI migration/report behavior
  item2  dedicated-module histories in place, listed historical upgrade, Alembic-base ledger
         downgrade/re-upgrade with data, the behind-head migrate-first guard
  item3  shared, foreign, incomplete, ledgerless and unknown layouts refused before any write
         (full pre/post snapshots)
  item4  runtime storage on PostgreSQL: ordinary storage, connection closure, rollback on an
         initialization failure (the SQLite part is probe_446_sqlite.py)
  item5  the published backup/stop/migrate guidance from a 0.1.16 database, the relocation SQL
         as published, concurrent first starts, service files; actionable errors, no repair
         (PgBouncer is probe_446_pooler.py; the release-by-release upgrade is probe_446_upgrade.py)

Migrations are subprocesses of the installed `openminion` console script; runtime storage
(build_runtime_storage) and the module stores are the installed factories, in process. Layouts
that earlier code wrote are built by --legacy-python (the published 0.1.16) in a child process,
with the same builders the PR's tests use. Catalog reads are the probe's own SQL. Evidence for
#446, not the implementation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sqlalchemy as sa  # noqa: E402

from _common import Checks, banner, quiet, sanitize, write_result  # noqa: E402
import probe_446_docsql as DOC  # noqa: E402
import probe_446_lib as L  # noqa: E402

PROBE = Path(__file__).resolve()
OBS: dict = {}
REFUSALS: list[dict] = []
LEGACY: str | None = None
H: L.Histories


def builder_name() -> str:
    return "0.1.16" if LEGACY else "the package under test"


def observe(key: str, value) -> None:
    OBS[key] = sanitize(value)
    print(f"OBS {key} | {json.dumps(sanitize(value), default=str)[:1500]}", flush=True)


def refusal(where: str, message: str) -> str:
    REFUSALS.append({"where": where, "message": message})
    return message


# ---------------------------------------------------------------------------- legacy builders

def run_child_action(action: str, args: list[str]) -> dict:
    """In-process builder (child mode, or no legacy interpreter given)."""
    quiet()
    if action == "legacy_runtime":
        return {"ok": True, "message": L.legacy_runtime(args[0])}
    if action == "legacy_module":
        ok, msg = L.attempt(lambda: L.legacy_module(args[0], args[1], args[2], downgrade=args[3] == "1"))
        return {"ok": ok, "message": msg}
    if action == "legacy_memory":
        ok, msg = L.attempt(lambda: L.legacy_memory(args[0], Path(args[1]), args[2]))
        return {"ok": ok, "message": msg}
    if action == "start_runtime":
        ok, msg = L.attempt(lambda: L.start_runtime(args[0], Path(args[1])))
        return {"ok": ok, "message": msg}
    if action == "open_store":
        ok, msg = L.attempt(lambda: L.open_close(args[0], args[1], Path(args[2])))
        return {"ok": ok, "message": msg}
    if action == "concurrent_store":
        start_at = float(args[3])
        while time.time() < start_at:
            time.sleep(0.002)
        ok, msg = L.attempt(lambda: L.open_close(args[0], args[1], Path(args[2])))
        return {"ok": ok, "message": msg}
    if action == "stopped_first_start":
        from openminion.modules.storage.runtime import module_store

        def stop(*_a, **_k):
            raise RuntimeError("stopped before om_meta")

        module_store.ensure_module_metadata_via_store = stop
        ok, msg = L.attempt(lambda: L.open_close(args[0], args[1], Path(args[2])))
        return {"ok": ok, "message": msg}
    return {"ok": False, "message": f"unknown action {action}"}


def legacy(action: str, *args: str, root: Path) -> dict:
    """A builder run by the published 0.1.16 (--legacy-python), else by this interpreter."""
    if LEGACY:
        return L.child(LEGACY, PROBE, action, *[str(a) for a in args], root=root)
    return run_child_action(action, [str(a) for a in args])


def legacy_cli_migrate(url: str, root: Path, module: str | None = None) -> dict:
    return L.migrate(url, root, module, python=LEGACY)


# ---------------------------------------------------------------------------- item 1

def module_ledgers_ok(url: str, modules=L.WORKING_MODULES) -> dict:
    snap = L.snapshot(url)
    out = {}
    for m in modules:
        led = snap.get(f"public__{m}", {}).get("ledgers", {})
        out[m] = {"alembic_version": led.get("alembic_version"), "module_id": (led.get("om_meta") or {}).get("module_id"),
                  "head": L.module_head(m), "listed": H.match(m, url, f"public__{m}")}
    return out


def ledgers_are_heads(info: dict) -> bool:
    return all(v["alembic_version"] == [v["head"]] and v["module_id"] == m for m, v in info.items())


def item1_1(c: Checks, work: Path) -> None:
    # --- runtime first
    url = L.new_db("i1_runtime_first")
    root = work / "i1-runtime-first"
    c.check("item1 runtime-first: runtime storage starts on a new database at v15", L.start_runtime(url, root / "rt1") == 15)
    L.runtime_rows(url)
    s0 = L.snapshot(url)
    run = L.migrate(url, root)
    c.check("item1 runtime-first: `openminion storage migrate --backend postgres --json` (all modules) exits 0",
            run["exit"] == 0, {"exit": run["exit"], "stderr": run["stderr"][-300:]})
    statuses = {m: (run["entries"].get(m) or {}).get("status") for m in L.WORKING_MODULES}
    c.check("item1 runtime-first: every PostgreSQL module reports ok", all(v == "ok" for v in statuses.values()), statuses)
    skipped = sorted(m for m, e in run["entries"].items() if e.get("status") == "skipped")
    observe("item1.skipped_modules", skipped)
    info = module_ledgers_ok(url)
    c.check("item1 runtime-first: each module's ledger is in public__<id>: one alembic_version row at its head, "
            "om_meta.module_id = <id>", ledgers_are_heads(info),
            {m: [v["alembic_version"], v["module_id"]] for m, v in info.items()})
    c.check("item1 runtime-first: each public__<id> holds exactly one listed current history of that module",
            all(v["listed"] for v in info.values()), {m: v["listed"] for m, v in info.items()})
    s1 = L.snapshot(url)
    c.check("item1 runtime-first: public (runtime storage's tables, columns, rows, indexes, sequences, ledger) "
            "is unchanged by the migration", s1["public"] == s0["public"], L.diff_snap(s0, s1)[:20])
    c.check("item1 runtime-first: no alembic_version or om_meta in public", not ({"alembic_version", "om_meta"}
                                                                                   & set(s1["public"]["tables"])),
            sorted(s1["public"]["tables"]))
    c.check("item1 runtime-first: runtime storage's history in public is listed (runtime v15)",
            H.match("runtime", url, "public") == "runtime v15", H.match("runtime", url, "public"))
    opened = {st: L.attempt(lambda st=st: L.open_close(st, url, root / f"store-{st}")) for st in L.STORES}
    c.check("item1 runtime-first: every module store opens on the same URL after the migration",
            all(ok for ok, _ in opened.values()), {k: (ok, L.short(m, 200)) for k, (ok, m) in opened.items()})
    # Round trips through the stores, then a second start of each.
    rt: dict = {}
    mem = L.memory_store(url, root / "mem")
    try:
        mem.put(L.memory_record("rec-1"))
        rt["memory"] = mem.get("rec-1") is not None
    finally:
        mem.close()
    audit = L.a2a_audit_store(url, root / "a2a")
    try:
        audit.append_audit(L.audit_record("msg-1"))
        rt["a2a_audit"] = [r.msg_id for r in audit.query_audit()] == ["msg-1"]
    finally:
        audit.close()
    sess = L.build_store("session_store", url, root / "sess")
    try:
        sid = sess.create_session(initial_agent_id="a", profile_version="v1", title="t", tags=[])
        rt["session"] = sess.get_session(sid) is not None
    finally:
        L.close(sess)
    c.check("item1 runtime-first: memory put/get, A2A audit append/query and session create/get round-trip",
            all(rt.values()), rt)
    rows = {"memory": L.scalars(url, 'SELECT id FROM "public__memory".memory_records'),
            "a2a": L.scalars(url, 'SELECT msg_id FROM "public__a2a".audit_records'),
            "session": len(L.scalars(url, 'SELECT session_id FROM "public__session".sessions'))}
    c.check("item1 runtime-first: those rows are on the server in public__memory, public__a2a and public__session",
            rows["memory"] == ["rec-1"] and rows["a2a"] == ["msg-1"] and rows["session"] == 1, rows)
    s2 = L.snapshot(url)
    c.check("item1 runtime-first: public is still unchanged after every store and round trip",
            s2["public"] == s0["public"], L.diff_snap(s0, s2)[:20])
    after_stores = module_ledgers_ok(url)
    c.check("item1 runtime-first: after the stores each public__<id> still holds one listed current history",
            all(v["listed"] for v in after_stores.values()) and ledgers_are_heads(after_stores),
            {m: v["listed"] for m, v in after_stores.items()})
    # Colliding names: one table per owner, each in its owner's schema.
    where = {}
    for name in L.COLLIDING:
        where[name] = sorted(s for s, b in s2.items() if name in b["tables"])
    runtime_sigs = L.signatures(url, "public")
    pub_ok = {n: (runtime_sigs.get(n) in H.runtime_signature(n)) for n in L.COLLIDING if n in runtime_sigs}
    c.check("item1 colliding names (sessions, events, idempotency_keys, memory_records, migrations, agents, "
            "session_turn_leases, session_retention_holds): each copy is in its own owner's schema; the public "
            "copies have runtime storage's listed v15 columns",
            all(pub_ok.values()) and all(all(s == "public" or s.startswith("public__") for s in v) for v in where.values()),
            {"schemas": where, "public_runtime_signature": pub_ok})
    one_ledger = {s: b["ledgers"] for s, b in s2.items() if s.startswith("public__")}
    c.check("item1 colliding ledgers: every module schema has exactly one alembic_version row and its own "
            "om_meta.module_id; public has none",
            all(len(v.get("alembic_version") or []) == 1 and (v.get("om_meta") or {}).get("module_id") == s.split("__", 1)[1]
                for s, v in one_ledger.items()) and "om_meta" not in s2["public"]["ledgers"],
            {s: [v.get("alembic_version"), (v.get("om_meta") or {}).get("module_id")] for s, v in one_ledger.items()})
    # Reopening.
    c.check("item1 reopen: runtime storage starts again at v15 with no new migration rows",
            L.start_runtime(url, root / "rt2") == 15 and len(L.snapshot(url)["public"]["ledgers"]["migrations"]) == 15)
    sa_ = L.snapshot(url, ignore_migrated_at=True)
    run2 = L.migrate(url, root)
    sb_ = L.snapshot(url, ignore_migrated_at=True)

    def heads_aside(snap: dict) -> tuple[dict, dict]:
        """The snapshot without om_meta's schema_head row and rows md5, and those heads apart."""
        out, heads = json.loads(json.dumps(snap)), {}
        for schema, block in out.items():
            meta = block.get("ledgers", {}).get("om_meta")
            if meta and "schema_head" in meta:
                heads[schema] = meta.pop("schema_head")
                block["tables"]["om_meta"].pop("md5", None)
        return out, heads

    xa, ha = heads_aside(sa_)
    xb, hb = heads_aside(sb_)
    rewritten = {s: [ha.get(s), hb.get(s)] for s in set(ha) | set(hb) if ha.get(s) != hb.get(s)}
    observe("item1.rerun_schema_head_rewrites", rewritten)
    c.check("item1 reopen: a second `storage migrate` exits 0, all ok, and changes nothing but om_meta's "
            "last_migrated_at and, where a store wrote its own head there, om_meta.schema_head back to the Alembic "
            "head (alembic_version, tables, rows and indexes unchanged)", run2["exit"] == 0 and all(
                (run2["entries"].get(m) or {}).get("status") == "ok" for m in L.WORKING_MODULES) and xa == xb
            and all(v[1] == L.module_head(s.split("__", 1)[1]) for s, v in rewritten.items()),
            {"exit": run2["exit"], "diff": L.diff_snap(xa, xb)[:20], "schema_head_rewritten": rewritten})
    again = {st: L.attempt(lambda st=st: L.open_close(st, url, root / f"store2-{st}")) for st in L.STORES}
    mem = L.memory_store(url, root / "mem2")
    try:
        found = mem.get("rec-1") is not None
    finally:
        mem.close()
    c.check("item1 reopen: every store opens again and the memory store reads rec-1 back",
            all(ok for ok, _ in again.values()) and found, {k: ok for k, (ok, _) in again.items()})
    # CLI migration/report behavior.
    plan = L.migrate(url, root, plan=True)
    plan_ok = {m: (plan["entries"].get(m) or {}).get("current_revision") == L.module_head(m)
               and (plan["entries"].get(m) or {}).get("status") == "plan" for m in L.WORKING_MODULES}
    c.check("item1 CLI: `storage migrate --plan --json` exits 0 and reports each module's head revision, read from "
            "its own schema", plan["exit"] == 0 and all(plan_ok.values()),
            {m: (plan["entries"].get(m) or {}).get("current_revision") for m in L.WORKING_MODULES})
    plan_text = L.migrate(url, root, plan=True, as_json=False)
    lines = plan_text["stdout"].splitlines()
    c.check("item1 CLI: `storage migrate --plan` (text) prints `<id>: revision=<head> (up to date)` for each module",
            plan_text["exit"] == 0 and all(f"  {m}: revision={L.module_head(m)} (up to date)" in lines
                                           for m in L.WORKING_MODULES), lines[:20])
    text_run = L.migrate(url, root, as_json=False)
    tl = text_run["stdout"].splitlines()
    c.check("item1 CLI: `storage migrate` (text) exits 0 and prints `  ok  <id>` for each module and "
            "`  skip  <id>: not validated for Postgres` for the others",
            text_run["exit"] == 0 and all(f"  ok  {m}" in tl for m in L.WORKING_MODULES)
            and all(f"  skip  {m}: not validated for Postgres" in tl for m in skipped), tl)
    verify = L.cli(["storage", "verify", "--backend", "postgres", "--postgres-url", url, "--json"], root)
    try:
        vstat = {e["module_id"]: e["status"] for e in json.loads(verify["stdout"])["modules"]}
    except Exception:  # noqa: BLE001
        vstat = {"raw": verify["stdout"][-300:]}
    c.check("item1 CLI: `storage verify --backend postgres --json` exits 0 with every module passed",
            verify["exit"] == 0 and all(vstat.get(m) == "passed" for m in L.WORKING_MODULES), vstat)
    c.check("item1 CLI: no command left a file in its working directory, and stderr was empty",
            all(not r["cwd_left"] for r in L.RUNS) and all(not r["stderr"].strip() for r in L.RUNS),
            [r["argv"][1:3] for r in L.RUNS if r["cwd_left"] or r["stderr"].strip()][:5])
    c.check("item1: no connection to the database is left after the runtime, stores and commands closed",
            L.wait_connections_zero(url) == 0, L.connections(url))



def item1_2(c: Checks, work: Path) -> None:
    # --- module first
    url2 = L.new_db("i1_module_first")
    root2 = work / "i1-module-first"
    run = L.migrate(url2, root2)
    c.check("item1 module-first: `storage migrate` on a new database exits 0, every module ok",
            run["exit"] == 0 and all((run["entries"].get(m) or {}).get("status") == "ok" for m in L.WORKING_MODULES),
            {m: (run["entries"].get(m) or {}).get("status") for m in L.WORKING_MODULES})
    info = module_ledgers_ok(url2)
    c.check("item1 module-first: each module is at its head in public__<id> with one listed current history, and "
            "public has no table", ledgers_are_heads(info) and all(v["listed"] for v in info.values())
            and not L.tables(url2, "public"), {m: v["listed"] for m, v in info.items()})
    opened = {st: L.attempt(lambda st=st: L.open_close(st, url2, root2 / f"store-{st}")) for st in L.STORES}
    c.check("item1 module-first: every module store opens", all(ok for ok, _ in opened.values()),
            {k: (ok, L.short(m, 200)) for k, (ok, m) in opened.items()})
    before = {s: b for s, b in L.snapshot(url2).items() if s != "public"}
    v = L.attempt(lambda: L.start_runtime(url2, root2 / "rt"))
    after = {s: b for s, b in L.snapshot(url2).items() if s != "public"}
    c.check("item1 module-first: runtime storage then starts at v15 in public with its listed v15 history",
            v == (True, "15") and H.match("runtime", url2, "public") == "runtime v15",
            {"start": v, "listed": H.match("runtime", url2, "public")})
    c.check("item1 module-first: runtime storage's start left every module schema unchanged", before == after,
            L.diff_snap(before, after)[:20])
    cols = L.column_names(url2, "public", "sessions")
    c.check("item1 module-first: public.sessions is runtime storage's (session_key, channel, target)",
            {"session_key", "channel", "target"} <= set(cols), cols)



def item1_3(c: Checks, work: Path) -> None:
    # --- the stores first, on the runtime URL, without `storage migrate`
    url3 = L.new_db("i1_stores_first")
    root3 = work / "i1-stores-first"
    L.start_runtime(url3, root3 / "rt")
    L.runtime_rows(url3)
    p0 = L.snapshot(url3)["public"]
    first = {st: L.attempt(lambda st=st: L.open_close(st, url3, root3 / f"first-{st}")) for st in L.STORES}
    nxt = {st: L.attempt(lambda st=st: L.open_close(st, url3, root3 / f"next-{st}")) for st in L.STORES}
    c.check("item1 stores-first: every module store's first start on the runtime URL succeeds, and so does the next",
            all(ok for ok, _ in first.values()) and all(ok for ok, _ in nxt.values()),
            {k: (first[k][0], nxt[k][0], L.short(first[k][1] or nxt[k][1], 200)) for k in L.STORES})
    listed = {m: H.match(m, url3, f"public__{m}") for m in sorted({L.store_module(s) for s in L.STORES})}
    c.check("item1 stores-first: each store's schema public__<id> holds one listed current history",
            all(listed.values()), listed)
    c.check("item1 stores-first: public is unchanged by the stores", L.snapshot(url3)["public"] == p0)
    pre = L.snapshot(url3)
    run = L.migrate(url3, root3)
    post = L.snapshot(url3)
    per = {}
    for m in L.WORKING_MODULES:
        e = run["entries"].get(m) or {}
        same = pre.get(f"public__{m}") == post.get(f"public__{m}")
        per[m] = {"status": e.get("status"), "unchanged": same, "error": L.short(" ".join(str(e.get("error", "")).split()), 220)}
    observe("item1.stores_first_then_migrate", per)
    c.check("item1 stores-first: `storage migrate` after the stores: each module ok, or an error with its "
            "schema unchanged", all(v["status"] == "ok" or (v["status"] == "error" and v["unchanged"])
                                    for v in per.values()), {m: (v["status"], v["unchanged"]) for m, v in per.items()})
    again = {st: L.attempt(lambda st=st: L.open_close(st, url3, root3 / f"again-{st}")) for st in L.STORES}
    c.check("item1 stores-first: every store still opens afterwards", all(ok for ok, _ in again.values()),
            {k: (ok, L.short(m, 200)) for k, (ok, m) in again.items()})



def item1_4(c: Checks, work: Path) -> None:
    # --- ledgers and tables of another owner in <base>__<id> are not adopted
    cases = {}
    u = L.new_db("i1_foreign_ledger")
    L.execute(u, 'CREATE SCHEMA "public__registry"',
              'CREATE TABLE "public__registry".om_meta (key TEXT PRIMARY KEY, value TEXT)',
              "INSERT INTO \"public__registry\".om_meta VALUES ('module_id', 'secret')",
              'CREATE TABLE "public__registry".alembic_version (version_num VARCHAR(32) PRIMARY KEY)',
              "INSERT INTO \"public__registry\".alembic_version VALUES ('0001_baseline')")
    cases["public__registry with secret's ledger"] = (u, "registry", "registry_store",
                                                      "records another owner's ledger: om_meta names 'secret'")
    u = L.new_db("i1_runtime_rows")
    L.execute(u, 'CREATE SCHEMA "public__memory"',
              'CREATE TABLE "public__memory".migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, '
              "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
              "INSERT INTO \"public__memory\".migrations (version, name) VALUES (1, 'bootstrap_core_tables')")
    cases["public__memory with runtime storage's ledger rows"] = (
        u, "memory", "memory_store", "records another owner's ledger: migrations holds runtime storage ledger rows")
    u = L.new_db("i1_foreign_tables")
    L.execute(u, "CREATE SCHEMA src")
    r = L.attempt(lambda: L.legacy_module(L.schema_url(u, "src"), "secret"))
    L.execute(u, 'ALTER SCHEMA "src" RENAME TO "public__registry"')
    cases["public__registry holding secret's tables and ledger"] = (
        u, "registry", "registry_store", "holds tables that are not registry's: secrets")
    observe("item1.foreign_build", {"secret baseline into src": r})
    for label, (u, module, store, fragment) in cases.items():
        before = L.snapshot(u)
        m1 = L.migrate(u, work / "i1-adopt", module)
        ok2, m2 = L.attempt(lambda u=u, store=store: L.open_close(store, u, work / f"i1-adopt-{store}"))
        after = L.snapshot(u)
        e1 = refusal(f"item1 {label} migrate", L.entry_error(m1, module))
        refusal(f"item1 {label} store", m2)
        c.check(f"item1 not adopted ({label}): `storage migrate --module {module}` exits 1 and the {store} is "
                f"refused, both with \"{fragment}\" and \"Nothing was written\"; the database is unchanged",
                m1["exit"] == 1 and not ok2 and fragment in e1 and fragment in m2 and L.NOTHING in e1
                and L.NOTHING in m2 and before == after,
                {"migrate": L.short(e1, 400), "store": L.short(m2, 300), "diff": L.diff_snap(before, after)})



def item1_5(c: Checks, work: Path) -> None:
    # --- the base schema follows the URL; names are checked
    u = L.new_db("i1_base_names")
    L.execute(u, 'CREATE SCHEMA "AppBase"', f'CREATE SCHEMA "{"b" * 60}"')
    r1 = L.migrate(L.with_options(u, '-c search_path="AppBase"'), work / "i1-base", "secret")
    led = L.snapshot(u).get("AppBase__secret", {}).get("ledgers", {})
    c.check("item1 base schema from the URL: with search_path=\"AppBase\", secret migrates into AppBase__secret",
            r1["exit"] == 0 and led.get("alembic_version") == ["0001_baseline"]
            and (led.get("om_meta") or {}).get("module_id") == "secret", {"exit": r1["exit"], "ledgers": led})
    r2 = L.migrate(L.with_options(u, f'-c search_path="{"b" * 60}"'), work / "i1-base", "secret")
    r3 = L.migrate(L.with_options(u, "-c search_path=missing_schema"), work / "i1-base", "secret")
    ok4, m4 = L.attempt(lambda: L.start_runtime(L.with_options(u, "-c search_path=missing_schema"), work / "i1-base-rt"))
    for where_, msg in (("long base", L.entry_error(r2, "secret")), ("missing base", L.entry_error(r3, "secret")),
                        ("missing base runtime", m4)):
        refusal(f"item1 {where_}", msg)
    c.check("item1 names checked: a 60-byte base is refused (\"longer than 63 bytes\"); a search_path naming no "
            "schema is refused for the module and for runtime storage",
            r2["exit"] == 1 and "longer than 63 bytes" in L.entry_error(r2, "secret") and r3["exit"] == 1
            and "search_path names no existing schema" in L.entry_error(r3, "secret") and not ok4
            and "search_path names no existing schema" in m4,
            {"long": L.short(L.entry_error(r2, "secret"), 300), "missing": L.short(L.entry_error(r3, "secret"), 200),
             "runtime": L.short(m4, 200)})



def item1_6(c: Checks, work: Path) -> None:
    # --- module connections keep the URL's options
    u = L.new_db("i1_options")
    url_opt = L.with_options(u, "-c statement_timeout=12345", application_name="om446_options")
    mem = L.memory_store(url_opt, work / "i1-opt")
    try:
        import sqlalchemy as sa

        with mem._engine.connect() as conn:  # noqa: SLF001 - the store's own engine
            vals = tuple(conn.execute(sa.text("SELECT current_schema(), current_setting('statement_timeout'), "
                                              "current_setting('application_name')")).one())
    finally:
        mem.close()
    c.check("item1 module connections: the memory store's connections use public__memory and keep the URL's "
            "statement_timeout and application_name", vals == ("public__memory", "12345ms", "om446_options"), vals)


# ---------------------------------------------------------------------------- item 2

def rows_kept(url: str, pre: dict, schema: str = "public") -> list[str]:
    """Tables whose pre-existing rows are not all still there over the columns they had."""
    lost = []
    for t, spec in pre.get(schema, {}).get("tables", {}).items():
        if t in L.LEDGER_TABLES:
            continue
        cols = [c[0] for c in spec["columns"]]
        if t not in L.tables(url, schema):
            lost.append(f"{t} gone")
            continue
        before_rows = pre.get("_rows", {}).get(f"{schema}.{t}")
        now = L.row_texts(url, schema, t, cols)
        if before_rows is not None and not set(before_rows) <= set(now):
            lost.append(f"{t} lost {len(set(before_rows) - set(now))}")
    return lost


def capture_rows(url: str, schema: str = "public") -> dict:
    """A snapshot plus every row of every table of `schema` (as ROW(...)::text) under "_rows"."""
    snap = L.snapshot(url)
    snap["_rows"] = {f"{schema}.{t}": L.row_texts(url, schema, t, [c[0] for c in spec["columns"]])
                     for t, spec in snap.get(schema, {}).get("tables", {}).items()}
    return snap


def json_rows(url: str, schema: str, table: str) -> list[dict]:
    return [dict(r) for r in L.scalars(url, f"SELECT row_to_json(x) FROM {L.qi(schema)}.{L.qi(table)} x")]


def table_defs(snap: dict, schema: str) -> dict:
    return {t: (v["columns"], v["constraints"]) for t, v in snap.get(schema, {}).get("tables", {}).items()}


def item2_0(c: Checks, work: Path) -> None:
    observe("item2.legacy_builder", builder_name())


def item2_1(c: Checks, work: Path) -> None:
    builder = builder_name()

    # --- a dedicated memory database written by the memory store of an earlier package
    url = L.new_db("i2_memory")
    root = work / "i2-memory"
    b = legacy("legacy_memory", url, root / "legacy", "rec-1", root=root / "child")
    pre = capture_rows(url)
    mem = L.memory_store(url, root / "now")
    try:
        got = mem.get("rec-1") is not None
        mem.put(L.memory_record("rec-2"))
    finally:
        mem.close()
    run = L.migrate(url, root, "memory")
    post = L.snapshot(url)
    c.check(f"item2 dedicated memory history (written by {builder}'s memory store in public): the memory store "
            "opens it in place, reads rec-1 and writes rec-2; `storage migrate --module memory` exits 0",
            b.get("ok") and got and run["exit"] == 0 and (run["entries"].get("memory") or {}).get("status") == "ok",
            {"build": b, "read": got, "migrate": run["exit"]})
    c.check("item2 dedicated memory history: no schema created, the ledger is unchanged (alembic 0004_namespace, "
            "om_meta.module_id memory): nothing moved or stamped",
            L.schemas(url) == ["public"] and post["public"]["ledgers"].get("alembic_version")
            == pre["public"]["ledgers"].get("alembic_version") == ["0004_namespace"]
            and (post["public"]["ledgers"].get("om_meta") or {}).get("module_id") == "memory",
            {"schemas": L.schemas(url), "ledger": post["public"]["ledgers"].get("alembic_version")})
    c.check("item2 dedicated memory history: every table definition is unchanged and every earlier row is "
            "still there (2 memory records now)",
            table_defs(pre, "public") == table_defs(post, "public") and not rows_kept(url, pre)
            and len(L.scalars(url, "SELECT id FROM public.memory_records")) == 2,
            {"lost": rows_kept(url, pre)})
    c.check("item2 dedicated memory history: it is memory's listed current history in public",
            H.match("memory", url, "public") is not None, H.match("memory", url, "public"))



def item2_2(c: Checks, work: Path) -> None:
    builder = builder_name()

    # --- a database where `storage migrate --module <id>` ran alone (0.1.16's console script)
    for module, store in (("secret", None), ("registry", "registry_store")):
        url = L.new_db(f"i2_alone_{module}")
        root = work / f"i2-alone-{module}"
        b = legacy_cli_migrate(url, root / "legacy", module)
        L.add_rows(url, "public")
        pre = capture_rows(url)
        run = L.migrate(url, root, module)
        ok_store = L.attempt(lambda: L.open_close(store, url, root / "store")) if store else (True, "")
        post = L.snapshot(url, ignore_migrated_at=True)
        c.check(f"item2 `storage migrate --module {module}` alone in public ({builder}): the installed migrate "
                + (f"and the {store} use it in place" if store else "uses it in place")
                + ": exit 0, no schema created, the ledger and every existing table definition unchanged, rows kept",
                b["exit"] == 0 and run["exit"] == 0 and ok_store[0] and L.schemas(url) == ["public"]
                and post["public"]["ledgers"].get("alembic_version") == pre["public"]["ledgers"].get("alembic_version")
                and all(table_defs(post, "public").get(t) == d_ for t, d_ in table_defs(pre, "public").items())
                and not rows_kept(url, pre) and any(pre["_rows"].values()),
                {"legacy": b["exit"], "migrate": run["exit"], "store": ok_store, "schemas": L.schemas(url),
                 "listed": H.match(module, url, "public"), "rows_before": {k: len(v) for k, v in pre["_rows"].items()},
                 "tables_added": sorted(set(table_defs(post, "public")) - set(table_defs(pre, "public"))),
                 "tables_changed": sorted(t for t, d_ in table_defs(pre, "public").items()
                                          if table_defs(post, "public").get(t) != d_)})



def item2_3(c: Checks, work: Path) -> None:
    builder = builder_name()

    # --- a listed historical session history upgrades in place
    url = L.new_db("i2_session_0004")
    root = work / "i2-session"
    b = legacy("legacy_module", url, "session", "0004_task_plan_step_start", "0", root=root / "child")
    L.execute(url, "DROP TABLE public.cron_scheduler_control")
    L.add_rows(url, "public")
    pre = capture_rows(url)
    hist = H.match("session", url, "public", current_only=False)
    run = L.migrate(url, root, "session")
    c.check("item2 listed historical session history (0004 without cron_scheduler_control, as 0.0.26..0.1.7 left "
            "it) is listed, and `storage migrate --module session` upgrades it in place to 0005",
            b.get("ok") and hist is not None and run["exit"] == 0 and L.schemas(url) == ["public"]
            and L.snapshot(url)["public"]["ledgers"].get("alembic_version") == ["0005_cron_scheduler_control"],
            {"before": hist, "exit": run["exit"], "error": L.short(L.entry_error(run, "session"), 300)})
    c.check("item2 listed historical session history: afterwards it is session's current listed history and every "
            "earlier row is kept", H.match("session", url, "public") is not None and not rows_kept(url, pre),
            {"after": H.match("session", url, "public"), "lost": rows_kept(url, pre)})



def item2_4(c: Checks, work: Path) -> None:
    builder = builder_name()

    # --- the Alembic base ledger, in the module's schema and in the base schema
    url = L.new_db("i2_base_ledger")
    root = work / "i2-base"
    L.start_runtime(url, root / "rt")
    L.runtime_rows(url)
    L.migrate(url, root)
    for m in L.WORKING_MODULES:
        L.add_rows(url, f"public__{m}")
    per: dict = {}
    for m in L.BASE_LISTED:
        others = {s: v for s, v in L.snapshot(url).items() if s != f"public__{m}"}
        d = L.attempt(lambda m=m: L.legacy_module(L.schema_url(url, f"public__{m}"), m, "base", downgrade=True))
        mid = L.snapshot(url).get(f"public__{m}", {})
        at_base = (mid.get("ledgers", {}).get("alembic_version") == [] and (mid.get("ledgers", {}).get("om_meta") or {})
                   .get("module_id") == m and set(mid.get("tables", {})) == {"alembic_version", "om_meta"})
        plan = L.migrate(url, root, m, plan=True)
        pe = plan["entries"].get(m) or {}
        run = L.migrate(url, root, m)
        after = L.snapshot(url)
        per[m] = {"downgrade": d[0], "at_base": at_base, "plan": [pe.get("current_revision"), pe.get("pending")],
                  "migrate": run["exit"], "head": (after.get(f"public__{m}", {}).get("ledgers", {})
                                                   .get("alembic_version")) == [L.module_head(m)],
                  "listed": H.match(m, url, f"public__{m}"),
                  "others_unchanged": {s: v for s, v in after.items() if s != f"public__{m}"} == others,
                  "error": L.short(L.entry_error(run, m) or d[1], 200)}
    c.check("item2 Alembic base ledger in public__<id> (each of the 11 listed modules): downgraded to base it holds "
            "only alembic_version (no row) and om_meta; `--plan` reports no revision (pending); `storage migrate` "
            "brings it back to its head and one listed current history in the same schema",
            all(v["downgrade"] and v["at_base"] and v["plan"] == [None, True] and v["migrate"] == 0 and v["head"]
                and v["listed"] for v in per.values()), per)
    c.check("item2 Alembic base ledger in public__<id>: every other schema (runtime storage's rows, the other "
            "modules' rows) is unchanged by each downgrade and re-upgrade",
            all(v["others_unchanged"] for v in per.values()), {m: v["others_unchanged"] for m, v in per.items()})
    per = {}
    for m in L.BASE_LISTED:
        u = L.new_db(f"i2_base_inplace_{m}")
        b1 = legacy("legacy_module", u, m, "head", "0", root=work / f"i2-bp-{m}")
        b2 = legacy("legacy_module", u, m, "base", "1", root=work / f"i2-bp-{m}")
        run = L.migrate(u, work / f"i2-bp-{m}", m)
        per[m] = {"built": bool(b1.get("ok") and b2.get("ok")), "migrate": run["exit"], "schemas": L.schemas(u),
                  "listed": H.match(m, u, "public"), "error": L.short(L.entry_error(run, m) or b2.get("message", ""), 200)}
    c.check(f"item2 Alembic base ledger in the base schema (each of the 11 modules, upgraded then downgraded to base "
            f"in public by {builder}): `storage migrate --module <id>` selects public in place (no public__<id>) and "
            "reaches the head's listed current history",
            all(v["built"] and v["migrate"] == 0 and v["schemas"] == ["public"] and v["listed"] for v in per.values()),
            per)
    # memory, session and skill: their downgrade fails on PostgreSQL, their base is not listed
    u = L.new_db("i2_base_unlisted")
    L.migrate(u, work / "i2-bu")
    downs = {m: L.attempt(lambda m=m: L.legacy_module(L.schema_url(u, f"public__{m}"), m, "base", downgrade=True))
             for m in ("memory", "session", "skill")}
    observe("item2.unlisted_base_downgrades", {m: [ok, L.short(msg, 200)] for m, (ok, msg) in downs.items()})
    u = L.new_db("i2_base_memory")
    L.execute(u, 'CREATE SCHEMA "public__memory"',
              'CREATE TABLE "public__memory".alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)',
              'CREATE TABLE "public__memory".om_meta (key TEXT PRIMARY KEY, value TEXT)',
              "INSERT INTO \"public__memory\".om_meta VALUES ('module_id', 'memory')")
    before = L.snapshot(u)
    run = L.migrate(u, work / "i2-bm", "memory")
    msg = refusal("item2 memory alembic base", L.entry_error(run, "memory"))
    c.check("item2 memory at Alembic base (not listed: its downgrade fails on PostgreSQL) is refused with "
            "\"alembic base, which is not a listed history\", nothing written",
            run["exit"] == 1 and "alembic base, which is not a listed history" in msg and L.NOTHING in msg
            and L.snapshot(u) == before, {"error": L.short(msg, 400), "downgrades": OBS.get("item2.unlisted_base_downgrades")})



def item2_5(c: Checks, work: Path) -> None:
    builder = builder_name()

    # --- one revision down and back up, with rows
    url = L.new_db("i2_down_up")
    root = work / "i2-down-up"
    L.migrate(url, root)
    per = {}
    for m in L.WORKING_MODULES:
        chain = L.module_chain(m)
        if len(chain) < 2:
            continue
        schema = f"public__{m}"
        L.add_rows(url, schema)
        pre = L.snapshot(url)
        pre_rows = {t: json_rows(url, schema, t) for t in pre[schema]["tables"] if t not in L.LEDGER_TABLES}
        d = L.attempt(lambda m=m, chain=chain, schema=schema: L.legacy_module(
            L.schema_url(url, schema), m, chain[-2], downgrade=True))
        if not d[0]:
            per[m] = {"downgrade": False, "error": L.short(d[1], 200)}
            continue
        mid_snap = L.snapshot(url)
        mid = mid_snap[schema]["tables"]
        run = L.migrate(url, root, m)
        post_snap = L.snapshot(url)
        post = post_snap[schema]["tables"]
        kept = {}
        for t, rows_ in pre_rows.items():
            if t in mid and t in post and rows_:
                common = [c_[0] for c_ in pre[schema]["tables"][t]["columns"]
                          if c_[0] in {x[0] for x in mid[t]["columns"]} and c_[0] in {x[0] for x in post[t]["columns"]}]
                now = json_rows(url, schema, t)
                proj = lambda rows, common=common: sorted(json.dumps([r.get(k) for k in common], default=str)  # noqa: E731
                                                          for r in rows)
                kept[t] = proj(rows_) == proj(now)
        err = L.entry_error(run, m)
        if run["exit"] != 0:
            refusal(f"item2 one revision cycle {m}", err)
        per[m] = {"downgrade": True, "to": chain[-2], "migrate": run["exit"],
                  "listed": H.match(m, url, schema), "rows_kept": all(kept.values()),
                  "tables_with_rows": len(kept), "not_kept": [t for t, v in kept.items() if not v],
                  "refused_unchanged": run["exit"] == 1 and L.NOTHING in err and mid_snap == post_snap,
                  "error": L.short(err, 400)}
    downgraded = {m: v for m, v in per.items() if v["downgrade"]}
    observe("item2.one_revision_cycle", per)
    back = {m: v for m, v in downgraded.items() if v["migrate"] == 0}
    c.check("item2 one revision down and back up in public__<id> (every module whose Alembic downgrade runs on "
            "PostgreSQL): `storage migrate` returns it to its head's listed current history with the rows of every "
            "table kept through the downgrade unchanged over the columns kept, or, where the downgrade left a layout "
            "that is not a listed history, refuses it with \"Nothing was written\" and the schema unchanged",
            bool(back) and all(v["listed"] and v["rows_kept"] for v in back.values())
            and all(v["refused_unchanged"] for m, v in downgraded.items() if m not in back)
            and sum(v["tables_with_rows"] for v in back.values()) > 0,
            {m: {k: v.get(k) for k in ("to", "migrate", "listed", "rows_kept", "tables_with_rows", "refused_unchanged",
                                        "error")} for m, v in per.items()})



def item2_6(c: Checks, work: Path) -> None:
    builder = builder_name()

    # --- the behind-head guard: `storage migrate` first, then the store
    for label, schema, module, target, store, head in (
            ("skill at 0003_audit in public", "public", "skill", "0003_audit", "skill_store", None),
            ("session at 0004 in public__session", "public__session", "session", "0004_task_plan_step_start",
             "session_store", None)):
        u = L.new_db(f"i2_behind_{module}")
        root = work / f"i2-behind-{module}"
        if schema != "public":
            L.execute(u, f'CREATE SCHEMA "{schema}"')
        b = legacy("legacy_module", L.schema_url(u, schema) if schema != "public" else u, module, target, "0",
                   root=root / "child")
        before = L.snapshot(u)
        ok1, m1 = L.attempt(lambda u=u, store=store, root=root: L.open_close(store, u, root / "s1"))
        after = L.snapshot(u)
        refusal(f"item2 behind head {label}", m1)
        want = (f"records alembic {target}, behind this code's alembic {L.module_head(module)}. Run `openminion "
                f"storage migrate --backend postgres --module {module}` before the module store opens it")
        c.check(f"item2 behind-head guard ({label}, built by {builder}): the {store} is refused with "
                f"\"{want}\" and \"Nothing was written\", the database unchanged, no connection left",
                b.get("ok") and not ok1 and want in m1 and L.NOTHING in m1 and before == after
                and L.wait_connections_zero(u) == 0, {"error": L.short(m1, 500), "diff": L.diff_snap(before, after)})
        run = L.migrate(u, root, module)
        ok2, m2 = L.attempt(lambda u=u, store=store, root=root: L.open_close(store, u, root / "s2"))
        led = L.snapshot(u).get(schema, {}).get("ledgers", {})
        c.check(f"item2 behind-head guard ({label}): after `storage migrate --module {module}` (exit 0) the store "
                f"opens; the ledger is the head in {schema} and the layout one listed current history",
                run["exit"] == 0 and ok2 and led.get("alembic_version") == [L.module_head(module)]
                and H.match(module, u, schema) is not None and set(L.schemas(u)) == ({"public", schema}),
                {"migrate": run["exit"], "store": (ok2, L.short(m2, 200)), "ledger": led.get("alembic_version"),
                 "schemas": L.schemas(u)})


# ---------------------------------------------------------------------------- item 3

def b_memory_then_failed_runtime(u, root):
    a = legacy("legacy_memory", u, root, "rec-1", root=root)
    b = legacy("legacy_runtime", u, root=root)
    return {"memory": a, "runtime": b, "expected": "v4" in str(b.get("message"))}


def b_a2a_then_failed_runtime(u, root):
    a = legacy("legacy_module", u, "a2a", "head", "0", root=root)
    b = legacy("legacy_runtime", u, root=root)
    return {"a2a": a, "runtime": b, "expected": "v1" in str(b.get("message"))}


def b_runtime_then_a2a(u, root):
    a = legacy("start_runtime", u, root / "rt", root=root)
    L.runtime_rows(u)
    b = legacy("legacy_module", u, "a2a", "head", "0", root=root)
    L.execute(u, "INSERT INTO public.audit_records(record_date, ts, msg_id, trace_id, from_agent, type, method, "
                 f"status) VALUES (current_date, '{L.NOW}', 'msg-0', 't', 'a', 'request', 'm', 'ok')")
    return {"runtime": a, "a2a": b}


def b_artifact_then_registry(u, root):
    return {"artifact": legacy("legacy_module", u, "artifact", "head", "0", root=root),
            "registry": legacy("legacy_module", u, "registry", "head", "0", root=root)}


def b_altered_runtime(u, root):
    a = legacy("start_runtime", u, root / "rt", root=root)
    L.execute(u, "ALTER TABLE public.sessions ADD COLUMN added_by_hand TEXT")
    return {"runtime": a}


def b_missing_runtime(u, root):
    a = legacy("start_runtime", u, root / "rt", root=root)
    L.runtime_rows(u)
    L.execute(u, "DROP TABLE public.room_participants CASCADE")
    return {"runtime": a}


def b_foreign_tables(u, root):
    L.execute(u, "CREATE TABLE public.sessions (id integer PRIMARY KEY, owner text)",
              "CREATE TABLE public.customers (id integer PRIMARY KEY)",
              "INSERT INTO public.sessions VALUES (1, 'someone else')")
    return {}


def b_foreign_migrations(u, root):
    L.execute(u, "CREATE TABLE public.migrations (version text PRIMARY KEY, name text)",
              "INSERT INTO public.migrations VALUES ('2024_01_init', 'another tool')")
    return {}


def b_two_locations(u, root):
    a = legacy("legacy_memory", u, root, "rec-1", root=root)
    L.execute(u, 'CREATE SCHEMA "public__memory"')
    return {"memory": a}


def b_registry_missing_table(u, root):
    a = legacy("legacy_module", u, "registry", "head", "0", root=root)
    L.add_rows(u, "public")
    L.execute(u, "DROP TABLE public.agent_status")
    return {"registry": a}


def b_unknown_revision(u, root):
    a = legacy("legacy_module", u, "secret", "head", "0", root=root)
    L.execute(u, "UPDATE public.alembic_version SET version_num = '0099_unknown'")
    return {"secret": a}


def b_unknown_store_head(u, root):
    # The registry store alone in a dedicated database, as earlier packages left it (store ledger).
    a = legacy("open_store", "registry_store", u, root / "rs", root=root)
    L.execute(u, "UPDATE public.om_meta SET value = '0099_unknown' WHERE key = 'schema_head'")
    return {"registry_store": a, "om_meta": L.snapshot(u)["public"]["ledgers"].get("om_meta")}


def b_lone_module_table_in_base(u, root):
    # public.secrets with exactly the columns this code's secret baseline creates, and no ledger.
    L.execute(u, "CREATE SCHEMA ref")
    r = L.attempt(lambda: L.legacy_module(L.schema_url(u, "ref"), "secret"))
    L.execute(u, "CREATE TABLE public.secrets (LIKE ref.secrets INCLUDING ALL)", "DROP SCHEMA ref CASCADE")
    L.add_rows(u, "public")
    return {"ref_baseline": r, "signature": L.signatures(u, "public")}


def b_core_events_in_module_schema(u, root):
    L.execute(u, 'CREATE SCHEMA "public__registry"',
              'CREATE TABLE "public__registry".core_events (foreign_payload text)',
              "INSERT INTO \"public__registry\".core_events VALUES ('someone else')")
    return {}


def b_one_store_table(u, root):
    L.execute(u, 'CREATE SCHEMA "public__registry"',
              'CREATE TABLE "public__registry".agent_methods (agent_id TEXT NOT NULL, method TEXT NOT NULL, '
              "quality_tier TEXT, cost_tier TEXT, latency_hint_ms INTEGER, PRIMARY KEY (agent_id, method))",
              "INSERT INTO \"public__registry\".agent_methods (agent_id, method) VALUES ('a', 'm')")
    return {}


def b_nonempty_without_ledger(u, root):
    # A complete registry store first start elsewhere, with rows; its om_meta removed; renamed in.
    L.execute(u, "CREATE SCHEMA src")
    ok, msg = L.attempt(lambda: L.open_close("registry_store", L.with_options(u, '-c search_path="src"'), root / "src"))
    src = "src__registry" if "src__registry" in L.schemas(u) else "src"  # 0.1.16 writes into src itself
    L.add_rows(u, src)
    L.execute(u, f'DROP TABLE "{src}".om_meta', f'ALTER SCHEMA "{src}" RENAME TO "public__registry"',
              *(["DROP SCHEMA src"] if src != "src" else []))
    return {"source_store": [ok, L.short(msg, 200)], "source_schema": src}


def b_first_start_stopped(u, root):
    # The package under test's registry store, its om_meta write stopped (a child process: the patch
    # must not reach this process's stores).
    return {"first_start": L.child(sys.executable, PROBE, "stopped_first_start", "registry_store", u,
                                   str(root / "first"), root=root)}


REG_FIRST = "agent_methods, agent_status, agents, core_events, core_sidecar_rows, sidecar_ingest_log"
LAYOUTS = {
    # name: (kind, builder, refused owners, message fragments, owners that still proceed)
    "memory-then-failed-runtime": ("shared", b_memory_then_failed_runtime, ["runtime", "memory", "memory_store"],
                                   ["migrations is recorded by runtime storage v3 and memory alembic 0004_namespace"],
                                   ["secret", "registry"]),
    "artifact-then-registry": ("shared", b_artifact_then_registry, ["artifact", "registry", "registry_store"],
                               ["agents is missing", "no artifact ledger"], ["secret", "runtime"]),
    "a2a-then-failed-runtime": ("shared", b_a2a_then_failed_runtime, ["runtime", "memory"],
                                ["no runtime storage ledger", "migrations matches no recorded ledger"], ["artifact"]),
    "runtime-then-a2a-audit-store": ("foreign", b_runtime_then_a2a, ["a2a", "a2a_audit_store"],
                                     ["idempotency_keys is recorded by runtime storage v15"],
                                     ["registry", "memory", "runtime"]),
    "foreign-tables": ("foreign", b_foreign_tables, ["runtime", "session"],
                       ["sessions matches no recorded ledger (its columns match no listed history)"], ["memory"]),
    "foreign-migrations-table": ("foreign", b_foreign_migrations, ["runtime", "memory"],
                                 ["migrations matches no recorded ledger (its columns match no listed history)"],
                                 ["secret"]),
    "arbitrary-core_events-in-public__registry": ("foreign", b_core_events_in_module_schema,
                                                   ["registry", "registry_store"],
                                                   ['"public__registry" holds tables (core_events) but records no '
                                                    "registry ledger"], ["secret", "runtime"]),
    "missing-runtime-table": ("incomplete", b_missing_runtime, ["runtime"], ["room_participants is missing"],
                              ["session", "memory", "a2a"]),
    "registry-history-missing-a-table": ("incomplete", b_registry_missing_table, ["registry", "registry_store"],
                                         ["agent_status is missing"], ["secret", "runtime"]),
    "first-start-stopped-before-om_meta": ("incomplete", b_first_start_stopped, ["registry", "registry_store"],
                                           [f'"public__registry" holds tables ({REG_FIRST}) but records no registry '
                                            "ledger"], ["secret"]),
    "lone-current-shaped-table-in-public__registry": ("ledgerless", b_one_store_table, ["registry", "registry_store"],
                                                      ['"public__registry" holds tables (agent_methods) but records '
                                                       "no registry ledger"], ["secret", "runtime"]),
    "lone-current-shaped-secrets-table-in-public": ("ledgerless", b_lone_module_table_in_base, ["secret"],
                                                    ["secrets matches no recorded ledger (its columns are listed for "
                                                     "secret)"], ["registry", "runtime"]),
    "nonempty-module-schema-without-ledger": ("ledgerless", b_nonempty_without_ledger, ["registry", "registry_store"],
                                              [f'"public__registry" holds tables ({REG_FIRST}) but records no '
                                               "registry ledger"], ["secret", "runtime"]),
    "unknown-alembic-revision": ("unknown", b_unknown_revision, ["secret"],
                                 ["its secret ledger records alembic 0099_unknown, which is not a listed history"],
                                 ["registry", "runtime"]),
    "unknown-store-head": ("unknown", b_unknown_store_head, ["registry", "registry_store"],
                           ["its registry ledger records store 0099_unknown, which is not a listed history"],
                           ["secret"]),
    "altered-runtime-table": ("unknown", b_altered_runtime, ["runtime", "session"],
                              ["sessions has other columns than runtime storage v15 lists",
                               "sessions matches no recorded ledger"], ["memory", "a2a"]),
    "two-locations": ("shared", b_two_locations, ["memory", "memory_store"], ["neither is selected"], ["secret"]),
}


def attempt_owner(owner: str, url: str, root: Path) -> tuple[bool, str, dict | None]:
    if owner == "runtime":
        ok, msg = L.attempt(lambda: L.start_runtime(url, root / f"rt-{uuid.uuid4().hex[:4]}"))
        return ok, msg, None
    if owner in L.STORES:
        ok, msg = L.attempt(lambda: L.open_close(owner, url, root / f"{owner}-{uuid.uuid4().hex[:4]}"))
        return ok, msg, None
    run = L.migrate(url, root, owner)
    return run["exit"] == 0, L.entry_error(run, owner), run


def item3_0(c: Checks, work: Path) -> None:
    kinds: dict = {}
    for name, (kind, build, refused, fragments, unaffected) in LAYOUTS.items():
        url = L.new_db(f"i3_{name[:20]}")
        root = work / f"i3-{name}"
        notes = L.attempt(lambda: build(url, root))
        before = L.snapshot(url)
        results = {o: attempt_owner(o, url, root) for o in refused}
        after = L.snapshot(url)
        conns = L.wait_connections_zero(url)
        msgs = {o: refusal(f"item3 {name} {o}", r[1]) for o, r in results.items()}
        joined = "\n".join(msgs.values())
        exits = {o: r[2]["exit"] for o, r in results.items() if r[2] is not None}
        text_check = True
        cli_mods = [o for o in refused if o not in L.STORES and o != "runtime"]
        if cli_mods:
            t = L.migrate(url, root, cli_mods[0], as_json=False)
            text_check = t["exit"] == 1 and any(line.startswith(f"  FAIL  {cli_mods[0]}: ") and L.NOTHING in line
                                                for line in t["stdout"].splitlines())
            after_text = L.snapshot(url)
        else:
            after_text = after
        ok = (not any(r[0] for r in results.values()) and before == after == after_text
              and all(f in joined for f in fragments) and all(L.NOTHING in m for m in msgs.values())
              and all(v == 1 for v in exits.values()) and text_check and conns == 0)
        kinds.setdefault(kind, []).append(name)
        c.check(f"item3 [{kind}] {name}: refused before any write for {', '.join(refused)}; every message has "
                "\"Nothing was written\" and the expected facts; `storage migrate` exits 1 (text: `  FAIL  <id>: "
                "...`); tables, columns, defaults, constraints, indexes, functions, triggers, sequences, ledgers "
                "and all rows are identical before and after; no connection left", ok,
                {"build": notes if not notes[0] else "ok", "refused": {o: [r[0], L.short(r[1], 450)] for o, r in results.items()},
                 "exits": exits, "text": text_check, "diff": L.diff_snap(before, after_text), "connections": conns,
                 "missing_fragments": [f for f in fragments if f not in joined]})
        public0 = L.snapshot(url).get("public")
        proceed = {}
        for o in unaffected:
            if o == "runtime":
                continue
            r = L.migrate(url, root, o)
            led = L.snapshot(url).get(f"public__{o}", {}).get("ledgers", {})
            proceed[o] = r["exit"] == 0 and (led.get("om_meta") or {}).get("module_id") == o
        public_same = L.snapshot(url).get("public") == public0
        if "runtime" in unaffected:
            proceed["runtime"] = L.attempt(lambda: L.start_runtime(url, root / "rt-unaffected")) == (True, "15")
        c.check(f"item3 [{kind}] {name}: owners that use none of the refused names still proceed "
                f"({', '.join(unaffected)}): modules into public__<id>, public untouched by them",
                all(proceed.values()) and public_same, {"proceed": proceed, "public_unchanged": public_same})
    observe("item3.kinds", kinds)
    # The storage engine's tables (core_events, core_sidecar_rows, sidecar_ingest_log) with an arbitrary shape in
    # the base schema. Runtime storage's StorageEngine creates them there; the listed runtime histories do not
    # name them, so the schema check does not read them (docs: engine tables are checked next to a module's
    # tables in <base>__<id>).
    per = {}
    for table in sorted(L.ENGINE_TABLES):
        url = L.new_db(f"i3_engine_{table[:12]}")
        L.execute(url, f"CREATE TABLE public.{table} (foreign_payload text)",
                  f"INSERT INTO public.{table} VALUES ('someone else')")
        before = L.snapshot(url)
        start_ = L.attempt(lambda url=url, table=table: L.start_runtime(url, work / f"i3-engine-{table}"))
        after = L.snapshot(url)
        per[table] = {"runtime_start": [start_[0], L.short(start_[1], 300)],
                      "columns_before": before["public"]["tables"][table]["columns"],
                      "columns_after": (after["public"]["tables"].get(table) or {}).get("columns"),
                      "rows_after": (after["public"]["tables"].get(table) or {}).get("rows"),
                      "diff": L.diff_snap(before, after)[:12]}
    observe("item3.engine_tables_in_public", per)
    c.check("item3 [foreign] an arbitrary-shaped storage engine table (core_events, core_sidecar_rows, "
            "sidecar_ingest_log) in the base schema: runtime storage's start writes nothing to it (refused before "
            "any write, or the table's definition and rows unchanged)",
            all(v["columns_before"] == v["columns_after"] and v["rows_after"] == 1 and
                (v["runtime_start"][0] or not v["diff"]) for v in per.values()),
            {t: {"start": v["runtime_start"], "after": v["columns_after"], "diff": v["diff"]} for t, v in per.items()})


# ---------------------------------------------------------------------------- item 4

def item4_0(c: Checks, work: Path) -> None:
    from openminion.modules.storage.runtime.context import build_runtime_storage

    url = L.new_db("i4_runtime")
    root = work / "i4"
    ctx = build_runtime_storage(root / "state.db", env={}, record_backend="record.postgres",
                                record_backend_options={"url": url})
    try:
        first = (ctx.migration_result.current_version, len(ctx.migration_result.applied_versions))
        s = ctx.sessions.resolve_session(agent_id="a", channel="cli", target="probe")
        ctx.sessions.append_message(session_id=s.id, role="user", body="hello 446")
        msgs = [m.body for m in ctx.sessions.list_messages(session_id=s.id)]
    finally:
        ctx.close()
    c1 = L.wait_connections_zero(url)
    ctx = build_runtime_storage(root / "state.db", env={}, record_backend="record.postgres",
                                record_backend_options={"url": url})
    try:
        second = (ctx.migration_result.current_version, len(ctx.migration_result.applied_versions))
        again = [m.body for m in ctx.sessions.list_messages(session_id=s.id)]
        got = ctx.sessions.get_session(s.id) is not None
    finally:
        ctx.close()
    c2 = L.wait_connections_zero(url)
    c.check("item4 runtime storage on PostgreSQL: a new database starts at v15 (15 applied), a session and a "
            "message are written and read; the next start applies nothing and reads them back",
            first == (15, 15) and second == (15, 0) and msgs == ["hello 446"] == again and got,
            {"first": first, "second": second, "messages": [msgs, again]})
    c.check("item4 lifecycle: after each RuntimeStorageContext.close() no connection to the database is left",
            c1 == 0 and c2 == 0, [c1, c2])
    rows = L.scalars(url, "SELECT body FROM public.messages")
    c.check("item4 runtime storage's rows are on the server in public.messages", rows == ["hello 446"], rows)

    # Rollback on an initialization failure: an index name runtime storage's v13 creates (indexes are not
    # read by the schema check, as the upgrade docs say), on another application's table.
    url = L.new_db("i4_init_failure")
    L.execute(url, "CREATE TABLE public.other_app (id integer PRIMARY KEY)",
              "INSERT INTO public.other_app VALUES (7)",
              "CREATE INDEX idx_event_run_facts_session ON public.other_app (id)")
    before = L.snapshot(url)
    ok, msg = L.attempt(lambda: L.start_runtime(url, work / "i4-fail"))
    after = L.snapshot(url)
    conns = L.wait_connections_zero(url)
    led = after["public"]["ledgers"].get("migrations") or []
    c.check("item4 rollback on an initialization failure: runtime storage's start fails at v13 "
            "(\"Migration v13 (add_event_run_facts) failed\"); v1..v12 stay committed, nothing of v13 "
            "(event_run_facts, its ledger row) remains, the other table, its row and index are unchanged, and no "
            "connection is left",
            not ok and "Migration v13 (add_event_run_facts) failed" in msg and [int(v) for v, _ in led] == list(range(1, 13))
            and "event_run_facts" not in after["public"]["tables"]
            and after["public"]["tables"]["other_app"] == before["public"]["tables"]["other_app"]
            and ["idx_event_run_facts_session", before_index(before)] in after["public"]["indexes"] and conns == 0,
            {"error": L.short(msg, 300), "versions": [v for v, _ in led], "connections": conns})
    L.execute(url, "DROP INDEX public.idx_event_run_facts_session")
    c.check("item4 rollback: once the operator removes that index, the next start resumes at v13 and reaches v15",
            L.attempt(lambda: L.start_runtime(url, work / "i4-fail2")) == (True, "15"))

    # A failed module migration leaves no schema (the migration transaction creates it).
    url = L.new_db("i4_failed_module")
    location = work / "i4-failing" / "migrations"
    (location / "versions").mkdir(parents=True, exist_ok=True)
    (location / "env.py").write_text(
        "from openminion.modules.storage.migrations.alembic import run_module_env\n"
        "run_module_env(module_id='secret', module_application_id=0x4F4D0006)\n", encoding="utf-8")
    (location / "versions" / "0001_fails.py").write_text(
        "from alembic import op\nrevision = '0001_fails'\ndown_revision = None\nbranch_labels = None\n"
        "depends_on = None\n\ndef upgrade():\n    op.execute('CREATE TABLE secrets (id text PRIMARY KEY)')\n"
        "    op.execute('SELECT 1 / 0')\n", encoding="utf-8")
    before = L.snapshot(url)

    def failing():
        import sqlalchemy as sa

        from openminion.modules.storage.migrations.module_ids import get_module_application_id
        from openminion.modules.storage.migrations.runner import MigrationRunner

        eng = sa.create_engine(url)
        try:
            return MigrationRunner(module_id="secret", db_path=work / "i4-failing" / "secret.db",
                                   module_application_id=get_module_application_id("secret"),
                                   alembic_script_location=location, backend_type="postgres", engine=eng,
                                   select_postgres_schema=True).migrate()
        finally:
            eng.dispose()

    rep = L.attempt(failing)
    after = L.snapshot(url)
    c.check("item4 rollback: a module migration that fails part-way (MigrationRunner with "
            "select_postgres_schema=True, as `storage migrate` builds it; a revision whose second statement divides "
            "by zero) reports the error and leaves no public__secret and nothing else",
            rep[0] and "division by zero" in rep[1] and "success=False" in rep[1] and before == after,
            {"report": L.short(rep[1], 300), "diff": L.diff_snap(before, after)})

    # A module store whose migration fails inside its transaction: the schema it created is rolled back and
    # its engine is disposed (an event trigger raises on the store's first table, as an injected failure).
    url = L.new_db("i4_store_failure")
    L.execute(url,
              "CREATE FUNCTION public.probe_446_fail() RETURNS event_trigger LANGUAGE plpgsql AS $$ BEGIN "
              "IF EXISTS (SELECT 1 FROM pg_event_trigger_ddl_commands() WHERE object_identity LIKE "
              "'%memory_records%' OR object_identity LIKE '%audit_records%') THEN RAISE EXCEPTION "
              "'probe 446: injected failure'; END IF; END $$",
              "CREATE EVENT TRIGGER probe_446_fail ON ddl_command_end EXECUTE FUNCTION public.probe_446_fail()")
    before = L.snapshot(url)
    res = {}
    for st in ("memory_store", "a2a_audit_store"):
        res[st] = L.attempt(lambda st=st: L.open_close(st, url, work / f"i4-sf-{st}"))
    mig = L.migrate(url, work / "i4-sf", "memory")
    after = L.snapshot(url)
    conns = L.wait_connections_zero(url)
    c.check("item4 rollback: the memory store, the A2A audit store and `storage migrate --module memory` each fail "
            "on the injected error; no public__memory or public__a2a is left, nothing else changed, and no "
            "connection is left (the factories dispose their engine)",
            not res["memory_store"][0] and not res["a2a_audit_store"][0] and mig["exit"] == 1
            and all("probe 446: injected failure" in m for _, m in res.values())
            and "probe 446: injected failure" in L.entry_error(mig, "memory") and before == after and conns == 0,
            {"stores": {k: L.short(m, 250) for k, (_, m) in res.items()}, "migrate": L.short(L.entry_error(mig, "memory"), 250),
             "diff": L.diff_snap(before, after), "connections": conns})
    L.execute(url, "DROP EVENT TRIGGER probe_446_fail")
    ok_after = {st: L.attempt(lambda st=st: L.open_close(st, url, work / f"i4-sf2-{st}"))[0]
                for st in ("memory_store", "a2a_audit_store")}
    c.check("item4 rollback: with the trigger dropped, both stores start", all(ok_after.values()), ok_after)


def before_index(snap: dict) -> str:
    return dict(map(tuple, snap["public"]["indexes"])).get("idx_event_run_facts_session", "")


# ---------------------------------------------------------------------------- item 5

def pg_dump(name: str, path: Path) -> int:
    import subprocess

    with open(path, "wb") as fh:
        return subprocess.run(["docker", "exec", "-i", "pg", "pg_dump", "-U", "postgres", "-Fc", "-d", name],
                              stdout=fh, stderr=subprocess.PIPE).returncode


def pg_restore(name: str, path: Path) -> int:
    import subprocess

    with open(path, "rb") as fh:
        return subprocess.run(["docker", "exec", "-i", "pg", "pg_restore", "-U", "postgres", "--no-owner", "-d", name],
                              stdin=fh, stderr=subprocess.PIPE).returncode


def item5_0(c: Checks, work: Path) -> None:
    root = work / "i5"
    bk = L.cli(["storage", "backup", "--backend", "postgres"], root)
    c.check("item5 backup guidance: `openminion storage backup --backend postgres` exits 0 and prints the "
            "pg_dump guidance", bk["exit"] == 0 and "pg_dump" in bk["stdout"], bk["stdout"].strip())
    c.check("item5 the refusal guidance names a section that the published guide has: \"PostgreSQL schemas per "
            "owner\" (docs/runtime-database-upgrades.md at v0.1.17, blob " + DOC.DOC_BLOB[:8] + ")",
            "## PostgreSQL schemas per owner" in DOC.HEADINGS and "### Upgrading" in DOC.HEADINGS)
    builder = builder_name()



def item5_1(c: Checks, work: Path) -> None:
    builder = builder_name()
    root = work / "i5"

    # --- A supported upgrade: runtime storage of 0.1.16 and a dedicated 0.1.16 memory database.
    rt_url, mem_url = L.new_db("i5_upgrade_runtime"), L.new_db("i5_upgrade_memory")
    b1 = legacy("start_runtime", rt_url, root / "old-rt", root=root / "c1")
    L.runtime_rows(rt_url)
    b2 = legacy("legacy_memory", mem_url, root / "old-mem", "rec-1", root=root / "c2")
    # 1. stop every process
    c.check(f"item5 upgrade step 1 (stop every process): the {builder} runtime and memory store have closed; no "
            "connection to either database", b1.get("ok") and b2.get("ok") and L.connections(rt_url) == 0
            and L.connections(mem_url) == 0, {"runtime": b1, "memory": b2})
    # 2. full pg_dump, restored into a scratch database
    dumps = {}
    for label, u in (("runtime", rt_url), ("memory", mem_url)):
        path = root / f"{label}.dump"
        root.mkdir(parents=True, exist_ok=True)
        rc = pg_dump(L.dbname(u), path)
        scratch = L.new_db(f"i5_scratch_{label}")
        rr = pg_restore(L.dbname(scratch), path)
        dumps[label] = {"dump": rc, "restore": rr, "equal": L.snapshot(scratch) == L.snapshot(u), "scratch": scratch,
                        "path": path}
    c.check("item5 upgrade step 2 (full pg_dump, checked by restoring it into a scratch database): both dumps "
            "restore, and each scratch database equals its source (tables, rows, indexes, ledgers)",
            all(v["dump"] == 0 and v["restore"] == 0 and v["equal"] for v in dumps.values()),
            {k: {x: y for x, y in v.items() if x in ("dump", "restore", "equal")} for k, v in dumps.items()})
    pre_rt, pre_mem = capture_rows(rt_url), L.snapshot(mem_url)
    # 3. upgrade every process; `storage migrate` before the module stores
    run = L.migrate(rt_url, root)
    runm = L.migrate(mem_url, root, "memory")
    v = L.attempt(lambda: L.start_runtime(rt_url, root / "new-rt"))
    mem = L.memory_store(mem_url, root / "new-mem")
    try:
        got = mem.get("rec-1") is not None
    finally:
        mem.close()
    stores = {st: L.attempt(lambda st=st: L.open_close(st, rt_url, root / f"new-{st}"))[0] for st in L.STORES}
    post_rt = L.snapshot(rt_url)
    c.check("item5 upgrade step 3: `storage migrate --backend postgres` on the runtime URL exits 0 (every module "
            "into public__<id>), `--module memory` on the dedicated memory database exits 0, runtime storage "
            "starts in place (v15), the memory store reads rec-1 in place, every store then opens",
            run["exit"] == 0 and runm["exit"] == 0 and v == (True, "15") and got and all(stores.values())
            and L.schemas(mem_url) == ["public"], {"migrate": run["exit"], "memory": runm["exit"], "runtime": v,
                                                   "read": got, "stores": stores})
    c.check("item5 upgrade step 3: runtime storage's tables and rows in public are unchanged by the upgrade "
            "(no migration row added, every row kept)",
            table_defs(pre_rt, "public") == table_defs(post_rt, "public") and not rows_kept(rt_url, pre_rt)
            and post_rt["public"]["ledgers"].get("migrations") == pre_rt["public"]["ledgers"].get("migrations"),
            L.diff_snap({"public": pre_rt["public"]}, {"public": post_rt["public"]}))
    # 4. going back: the previous package on the restored pre-upgrade dump
    if LEGACY:
        back = {"runtime": legacy("start_runtime", dumps["runtime"]["scratch"], root / "back-rt", root=root / "c-b1"),
                "memory": legacy("legacy_memory", dumps["memory"]["scratch"], root / "back-mem", "rec-9",
                                 root=root / "c-b2")}
        c.check("item5 upgrade step 4 (going back): 0.1.16 opens the restored pre-upgrade dumps (runtime storage "
                "starts, the memory store writes)", all(b.get("ok") for b in back.values()), back)



def item5_2(c: Checks, work: Path) -> None:
    builder = builder_name()
    root = work / "i5"

    # --- What 0.1.16's documented `storage migrate --backend postgres` leaves on the runtime URL (several modules
    # behind one alembic_version): a historical layout. Observed and checked for "nothing written", not certified.
    u = L.new_db("i5_shared_0116")
    legacy("start_runtime", u, root / "s-rt", root=root / "c3")
    L.runtime_rows(u)
    old = legacy_cli_migrate(u, root / "s-old")
    before = L.snapshot(u)
    new = L.migrate(u, root / "s-new")
    rt = L.attempt(lambda: L.start_runtime(u, root / "s-rt2"))
    after = L.snapshot(u)
    statuses = {m: (new["entries"].get(m) or {}).get("status") for m in L.WORKING_MODULES}
    errs = {m: L.entry_error(new, m) for m, s in statuses.items() if s == "error"}
    for m, e in errs.items():
        refusal(f"item5 shared 0.1.16 layout {m}", e)
    public_same = before["public"] == after["public"]
    observe("item5.shared_0116_layout", {"0.1.16 migrate exit": old["exit"],
                                         "0.1.16 statuses": {m: (old["entries"].get(m) or {}).get("status")
                                                             for m in L.WORKING_MODULES},
                                         "0.1.17 statuses": statuses, "runtime": [rt[0], L.short(rt[1], 300)],
                                         "new schemas": sorted(set(after) - set(before)),
                                         "errors": {m: L.short(e, 300) for m, e in errs.items()}})
    c.check(f"item5 the layout {builder}'s `storage migrate --backend postgres` leaves on the runtime URL (modules "
            "behind one alembic_version; listed in the docs as refused unless proven): every module the installed "
            "migrate does not prove is refused with \"Nothing was written\", public is unchanged, the others get "
            "public__<id>", all(L.NOTHING in e for e in errs.values()) and public_same
            and all(s in ("ok", "error") for s in statuses.values())
            and all(after.get(f"public__{m}") for m, s in statuses.items() if s == "ok"),
            {"refused": sorted(errs), "public_unchanged": public_same, "runtime_start": rt[0]})



def item5_3(c: Checks, work: Path) -> None:
    builder = builder_name()
    root = work / "i5"

    # --- The published relocation SQL, run as written (R1 and its inverse, R1b, R2 park and copy)
    u = L.new_db("i5_r1")
    legacy("legacy_memory", u, root / "r1", "rec-1", root=root / "c4")
    original = L.snapshot(u)
    ok0, m0 = L.attempt(lambda: L.start_runtime(u, root / "r1-rt0"))
    refusal("item5 R1 runtime before relocation", m0)
    L.execute(u, *DOC.statements("R1"))
    L.execute(u, *DOC.statements("R1-inverse"))
    inv = L.snapshot(u) == original
    L.execute(u, *DOC.statements("R1"))
    mem = L.memory_store(u, root / "r1-mem")
    try:
        got = mem.get("rec-1") is not None
        mem.put(L.memory_record("rec-2"))
    finally:
        mem.close()
    rt = L.attempt(lambda: L.start_runtime(u, root / "r1-rt1"))
    led = L.snapshot(u).get("public__memory", {}).get("ledgers", {})
    c.check("item5 relocation R1 as published: runtime storage is first refused on the dedicated memory history "
            "(\"not provably its own\"); R1 then R1-inverse restores the original exactly; after R1 the memory "
            "store reads rec-1 in public__memory, runtime storage starts in public (v15), the memory ledger is "
            "unchanged", not ok0 and "not provably its own" in m0 and inv and got and rt == (True, "15")
            and led.get("alembic_version") == ["0004_namespace"] and L.schemas(u) == ["public", "public__memory"],
            {"refused": L.short(m0, 250), "inverse_equal": inv, "read": got, "runtime": rt, "schemas": L.schemas(u)})
    u = L.new_db("i5_r1b")
    b_memory_then_failed_runtime(u, root / "r1b")
    L.execute(u, *DOC.statements("R1b"))
    rt = L.attempt(lambda: L.start_runtime(u, root / "r1b-rt"))
    mem = L.memory_store(u, root / "r1b-mem")
    try:
        got = mem.get("rec-1") is not None
    finally:
        mem.close()
    c.check("item5 relocation R1b as published (memory first, then runtime storage committed v1..v3): runtime "
            "storage resumes and reaches v15, the memory store reads rec-1 in public__memory",
            rt == (True, "15") and got, {"runtime": rt, "read": got})
    u = L.new_db("i5_r2")
    b_runtime_then_a2a(u, root / "r2")
    keys = L.snapshot(u)["public"]["tables"]["idempotency_keys"]
    L.execute(u, *DOC.statements("R2-park"))
    r = L.migrate(u, root / "r2", "a2a")
    L.execute(u, *DOC.statements("R2-copy"))
    counts = {t: (L.scalars(u, f"SELECT count(*) FROM a2a_hold.{t}")[0],
                  L.scalars(u, f'SELECT count(*) FROM "public__a2a".{t}')[0])
              for t in ("audit_records", "a2a_audit_archive", "agents", "jobs")}
    audit = L.a2a_audit_store(u, root / "r2-audit")
    try:
        audit.append_audit(L.audit_record("msg-1"))
        ids = sorted(rec.msg_id for rec in audit.query_audit({"limit": 10}))
    finally:
        audit.close()
    rt = L.attempt(lambda: L.start_runtime(u, root / "r2-rt"))
    c.check("item5 relocation R2 as published (runtime storage, then the A2A audit store adopted idempotency_keys): "
            "park, `storage migrate --module a2a` (exit 0), copy; every parked table's row count equals the copy's; "
            "the A2A audit store reads msg-0 and writes msg-1; runtime storage's idempotency_keys is unchanged and "
            "it starts (v15)",
            r["exit"] == 0 and all(a == b for a, b in counts.values()) and ids == ["msg-0", "msg-1"]
            and L.snapshot(u)["public"]["tables"]["idempotency_keys"] == keys and rt == (True, "15"),
            {"migrate": r["exit"], "counts": counts, "audit": ids, "runtime": rt})



def item5_4(c: Checks, work: Path) -> None:
    builder = builder_name()
    root = work / "i5"

    # --- Concurrent first starts (documented: a second process may be refused; a later start succeeds)
    outcomes: dict = {"registry_store": [], "memory_store": [], "storage migrate --module secret": []}
    later_ok, layouts_ok, others = [], [], []
    for rnd in range(6):
        u = L.new_db(f"i5_race_{rnd}")
        start_at = time.time() + 2.5
        import subprocess

        procs = []
        for st in ("registry_store", "registry_store", "registry_store", "memory_store", "memory_store",
                   "memory_store"):
            procs.append((st, subprocess.Popen(
                [sys.executable, "-I", "-X", "utf8", str(PROBE), "--child", "concurrent_store", st, u,
                 str(root / f"race{rnd}-{st}-{uuid.uuid4().hex[:4]}"), str(start_at)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(work))))
        for i in range(3):
            procs.append(("storage migrate --module secret", subprocess.Popen(
                [L.console(), "storage", "migrate", "--backend", "postgres", "--postgres-url", u, "--module", "secret",
                 "--json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(work))))
        for kind, p in procs:
            out, err = p.communicate(timeout=600)
            if kind.startswith("storage"):
                try:
                    e = json.loads(out)["modules"][0]
                    res = {"ok": p.returncode == 0, "message": " ".join(str(e.get("error", "")).split())}
                except Exception:  # noqa: BLE001
                    res = {"ok": False, "message": f"exit {p.returncode}: {(out + err)[-400:]}"}
            else:
                line = [x for x in out.splitlines() if x.startswith("CHILD_RESULT ")]
                res = json.loads(line[-1][13:]) if line else {"ok": False, "message": err[-400:]}
            outcomes[kind].append(res)
        later = {st: L.attempt(lambda st=st: L.open_close(st, u, root / f"race{rnd}-later-{st}"))[0]
                 for st in ("registry_store", "memory_store")}
        later["secret"] = L.migrate(u, root / f"race{rnd}-later", "secret")["exit"] == 0
        later_ok.append(all(later.values()))
        layouts_ok.append(all(H.match(m, u, f"public__{m}") for m in ("registry", "memory", "secret")))
        others.append(sorted(set(L.schemas(u)) - {"public", "public__registry", "public__memory", "public__secret"})
                      + L.tables(u, "public"))
    summary = {k: {"ok": sum(1 for r in v if r["ok"]), "failed": [L.short(r["message"], 260) for r in v if not r["ok"]]}
               for k, v in outcomes.items()}
    observe("item5.concurrent_first_starts", summary)
    failures = [r["message"] for v in outcomes.values() for r in v if not r["ok"]]
    documented = [m for m in failures if L.NOTHING in m]
    observe("item5.concurrent_failure_kinds", {
        "documented refusal (Nothing was written)": len(documented),
        "UniqueViolation on CREATE SCHEMA": sum(1 for m in failures if "UniqueViolation" in m and "CREATE SCHEMA" in m),
        "UniqueViolation on CREATE TABLE/INDEX IF NOT EXISTS": sum(
            1 for m in failures if "UniqueViolation" in m and "CREATE SCHEMA" not in m),
        "other": [L.short(m, 200) for m in failures if L.NOTHING not in m and "UniqueViolation" not in m],
        "attempts": sum(len(v) for v in outcomes.values())})
    c.check("item5 concurrent first starts (6 rounds, each a new database: 3 registry stores, 3 memory stores and 3 "
            "`storage migrate --module secret` started together): every failure is a refusal with \"Nothing was "
            "written\" (the documented first-start limitation)", len(documented) == len(failures),
            {"outcomes": summary, "undocumented": [L.short(m, 300) for m in failures if L.NOTHING not in m][:6]})
    c.check("item5 concurrent first starts: in every round a later start of each store and a later migrate succeed, "
            "each module schema holds one listed current history, and nothing was created elsewhere",
            all(later_ok) and all(layouts_ok) and not any(others), {"later": later_ok, "layouts": layouts_ok,
                                                                     "elsewhere": others})



def item5_5(c: Checks, work: Path) -> None:
    builder = builder_name()
    root = work / "i5"

    # --- A first start stopped before om_meta stays refused: never completed, adopted or dropped
    u = L.new_db("i5_stopped")
    b_first_start_stopped(u, root / "stopped")
    before = L.snapshot(u)
    tries = [L.attempt(lambda i=i: L.open_close("registry_store", u, root / f"stopped-{i}")) for i in range(2)]
    run = L.migrate(u, root / "stopped", "registry")
    c.check("item5 a first start stopped before om_meta: two later starts and `storage migrate --module registry` "
            "are refused with \"records no registry ledger\" and \"take a full pg_dump\", and the schema stays "
            "exactly as the stopped start left it (no automatic completion, adoption or drop)",
            not any(ok for ok, _ in tries) and run["exit"] == 1 and all("records no registry ledger" in m and
                                                                         "take a full pg_dump" in m for _, m in tries)
            and "records no registry ledger" in L.entry_error(run, "registry") and L.snapshot(u) == before,
            {"stores": [L.short(m, 200) for _, m in tries], "migrate": L.short(L.entry_error(run, "registry"), 200)})



def item5_6(c: Checks, work: Path) -> None:
    builder = builder_name()
    root = work / "i5"

    # --- Role without CREATE: the messages give the statements to run
    import sqlalchemy as sa

    u = L.new_db("i5_role")
    role = f"om446_app_{uuid.uuid4().hex[:6]}"
    eng = L.admin()
    with eng.connect() as conn:
        conn.execute(sa.text(f"CREATE ROLE {role} LOGIN PASSWORD 'om446'"))
    eng.dispose()
    try:
        L.execute(u, f'GRANT CONNECT ON DATABASE "{L.dbname(u)}" TO {role}',
                  f'CREATE SCHEMA "public__memory" AUTHORIZATION {role}', 'CREATE SCHEMA "public__secret"',
                  'CREATE TABLE "public__secret".alembic_version (version_num varchar(32) PRIMARY KEY)')
        role_url = sa.engine.make_url(u).set(username=role, password="om446").render_as_string(hide_password=False)
        before = L.snapshot(u)
        mem = L.memory_store(role_url, root / "role-mem")
        try:
            mem.put(L.memory_record("rec-1"))
        finally:
            mem.close()
        no_grant = L.entry_error(L.migrate(role_url, root / "role", "secret"), "secret")
        no_create = L.entry_error(L.migrate(role_url, root / "role", "registry"), "registry")
        refusal("item5 role without USAGE", no_grant)
        refusal("item5 role without CREATE", no_create)
        after = L.snapshot(u)
        c.check("item5 permissions: a role without CREATE uses the empty public__memory a DBA created for it; the "
                f"refusals give `GRANT USAGE, CREATE ON SCHEMA \"public__secret\" TO \"{role}\"` and `CREATE SCHEMA "
                f"\"public__registry\" AUTHORIZATION \"{role}\"`, and write nothing",
                (after.get("public__memory", {}).get("ledgers", {}).get("alembic_version") == ["0004_namespace"])
                and f'GRANT USAGE, CREATE ON SCHEMA "public__secret" TO "{role}"' in no_grant
                and f'CREATE SCHEMA "public__registry" AUTHORIZATION "{role}"' in no_create
                and after["public__secret"] == before["public__secret"] and "public__registry" not in after,
                {"no_grant": L.short(no_grant, 300), "no_create": L.short(no_create, 300)})
    finally:
        L.execute(u, f"DROP OWNED BY {role}")
        eng = L.admin()
        with eng.connect() as conn:
            conn.execute(sa.text(f"DROP DATABASE IF EXISTS {L.qi(L.dbname(u))} WITH (FORCE)"))
            conn.execute(sa.text(f"DROP ROLE IF EXISTS {role}"))
        eng.dispose()



def item5_7(c: Checks, work: Path) -> None:
    builder = builder_name()
    root = work / "i5"

    # --- Service files: an `options` value from a service file does not reach module connections
    u = L.new_db("i5_service")
    pu = sa.engine.make_url(u)
    svc = root / "pg_service.conf"
    svc.write_text(f"[om446]\nhost={pu.host}\nport={pu.port or 5432}\nuser={pu.username}\npassword={pu.password}\n"
                   f"dbname={pu.database}\noptions=-c statement_timeout=4242\n", encoding="utf-8")
    os.environ["PGSERVICEFILE"] = str(svc)
    try:
        svc_url = "postgresql+psycopg:///?service=om446"
        eng = sa.create_engine(svc_url)
        with eng.connect() as conn:
            plain = str(conn.execute(sa.text("SHOW statement_timeout")).scalar())
        eng.dispose()
        mem = L.memory_store(svc_url, root / "svc-mem")
        try:
            with mem._engine.connect() as conn:  # noqa: SLF001
                module = tuple(conn.execute(sa.text("SELECT current_schema(), current_setting('statement_timeout')")).one())
        finally:
            mem.close()
    except Exception as exc:  # noqa: BLE001
        plain, module = f"error {exc}", ()
    finally:
        os.environ.pop("PGSERVICEFILE", None)
    c.check("item5 service files (documented): a connection with the service-file URL gets the service's "
            "statement_timeout (4242ms); the memory store's module connection uses public__memory and does not "
            "(the module URL gives options)", plain == "4242ms" and module[:1] == ("public__memory",)
            and module[1:] != ("4242ms",), {"plain": plain, "module": module})



def item5_8(c: Checks, work: Path) -> None:
    builder = builder_name()
    root = work / "i5"

    # --- Every refusal recorded by this probe is actionable and wrote nothing (checked per case above)
    msgs = [r for r in REFUSALS if r["message"]]
    actionable = []
    for r in msgs:
        m = r["message"]
        how = ("take a full pg_dump" in m and L.GUIDANCE in m) or any(k in m for k in (
            "GRANT USAGE, CREATE ON SCHEMA", "CREATE SCHEMA", "Run `openminion storage migrate", "Use a base schema",
            "Connect module stores directly", "search_path names no existing schema"))
        named = '"' in m or "search_path" in m or "PostgreSQL schema '" in m
        actionable.append({"where": r["where"], "nothing_written": L.NOTHING in m, "names_schema": named, "how": how})
    bad = [a for a in actionable if not (a["nothing_written"] and a["names_schema"] and a["how"])]
    observe("item5.refusal_count", len(actionable))
    c.check(f"item5 actionable errors: every refusal message this probe collected ({len(actionable)}) says "
            "\"Nothing was written\", names the schema, and gives the next step (stop, pg_dump and the guide's "
            "section; or the GRANT / CREATE SCHEMA / `storage migrate` / base-name step)",
            bool(actionable) and not bad, bad[:10])


# ---------------------------------------------------------------------------- main

SECTIONS = ("item1", "item2", "item3", "item4", "item5")


def parts(name: str) -> list:
    found = sorted((int(k.rsplit("_", 1)[1]), v) for k, v in globals().items()
                   if k.startswith(name + "_") and k.rsplit("_", 1)[1].isdigit() and callable(v))
    return [f for _, f in found]


def main() -> int:
    global LEGACY, H
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("out")
    ap.add_argument("--legacy-python")
    ap.add_argument("--histories")
    ap.add_argument("--only", default=",".join(SECTIONS))
    a = ap.parse_args()
    quiet()
    LEGACY = a.legacy_python
    import openminion

    installed = Path(openminion.__file__).parent / "modules/storage/migrations/postgres_histories.json"
    hist_path = Path(a.histories) if a.histories else installed
    H = L.Histories(hist_path)
    info = banner("probe_446", server=L.server_version(), histories=str(hist_path), histories_sha256=H.sha256,
                  histories_installed=installed.exists(), legacy_python=LEGACY)
    if LEGACY:
        import subprocess

        info["legacy_version"] = subprocess.run([LEGACY, "-I", "-c", "import openminion;print(openminion.__version__)"],
                                                capture_output=True, text=True).stdout.strip()
        print("legacy builder version:", info["legacy_version"])
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    c = Checks()
    for name in a.only.split(","):
        started = time.monotonic()
        print(f"=== {name}", flush=True)
        for fn in parts(name):
            try:
                fn(c, work / name)
            except Exception as exc:  # noqa: BLE001
                c.check(f"{fn.__name__} ran to the end", False,
                        f"{type(exc).__name__}: {exc} | {traceback.format_exc()[-1500:]}")
        print(f"=== {name} done in {time.monotonic() - started:.0f}s", flush=True)
    dropped = L.drop_all()
    payload = {"banner": info, "summary": c.summary(), "checks": c.items, "observations": OBS,
               "refusals": REFUSALS, "commands": L.RUNS, "databases_dropped": dropped}
    write_result(Path(a.out), payload)
    s = c.summary()
    print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
    return 0 if c.all_pass else 1


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        res = run_child_action(sys.argv[2], sys.argv[3:])
        print("CHILD_RESULT " + json.dumps(res, default=str), flush=True)
        sys.exit(0)
    sys.exit(main())
