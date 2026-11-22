"""#446 after #817 on the installed package, PostgreSQL: the two 0.1.17 gaps and their neighbors.

    python -I -X utf8 probe_446_0124.py WORK OUT.json --legacy-python PY0116 --prev-python PY0123
                                        [--only engine,collision,report,histories,neighbors]

Runs from an empty directory against OPENMINION_TEST_POSTGRES_URL (a superuser URL), a new
database per case. Sections follow the maintainer's acceptance list for #446 on 21/11
(issue comment 7061088128):

  engine      foreign or unowned PostgreSQL storage engine tables (core_events, core_sidecar_rows,
              sidecar_ingest_log) in the base schema are refused before any DDL: runtime storage,
              in-place module selection (`storage migrate`, the registry store) and an engine
              handed to a module directly (MigrationRunner without schema selection)
  collision   concurrent first-start namespace collisions: deterministic competing CREATE SCHEMA
              (an uncommitted competitor that commits once the start is blocked on it: 23505 on
              pg_namespace_nspname_index; a competitor committed just before the migration's
              CREATE SCHEMA: 42P06), on the store path and the migration path, then free-running
              rounds of concurrent first starts
  report      the runtime error and the migration report say what happened: SchemaOwnershipError
              with "Nothing was written" from the stores; a failed MigrationReport with
              rolled_back=True after a begun transaction, rolled_back=False for a refusal before it,
              before/after states that match the server
  histories   proven histories unchanged and accepted: runtime databases written by 0.1.16 and
              0.1.23, a full 0.1.23 database (runtime plus every module) upgraded in place, the
              listed-history file a superset of 0.1.23's
  neighbors   assertions kept from the 0.1.17 verification: foreign tables in a module schema,
              a foreign `sessions`, an unrelated table, a role without CREATE, an injected
              first-start failure rolled back, no connection left

Migrations are subprocesses of the installed `openminion` console script; runtime storage
(build_runtime_storage) and the module stores are the installed factories. Layouts that earlier
releases wrote are built by those releases in their own venvs (--legacy-python 0.1.16,
--prev-python 0.1.23). Catalog reads are the probe's own SQL (probe_446_lib.py, byte copy from
the 0.1.17 verification). The competitor connections and the statement hook only add a second
process's CREATE SCHEMA at a chosen moment; no openminion code is patched.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy import text  # noqa: E402

from _common import Checks, banner, quiet, sanitize, write_result  # noqa: E402
import probe_446_lib as L  # noqa: E402

PROBE = Path(__file__).resolve()
OBS: dict = {}
LEGACY: str | None = None
PREV: str | None = None
H: L.Histories
HEAD: int = 0
COMPETING = "is being initialized by another concurrent process"
OTHER_COLUMNS = "other columns than this code's storage engine creates"


def observe(key: str, value) -> None:
    OBS[key] = sanitize(value)
    print(f"OBS {key} | {json.dumps(sanitize(value), default=str)[:1500]}", flush=True)


# ---------------------------------------------------------------------------- child actions

def runner_report(url: str, module_id: str) -> dict:
    """The MigrationRunner `storage migrate` builds (the CLI's own factory), its report as data."""
    from openminion.cli.commands.storage import _make_runner, _resolve_postgres_engine

    engine = _resolve_postgres_engine("postgres", url)
    try:
        runner = _make_runner(module_id, ":memory:", backend_type="postgres", engine=engine)
        rep = runner.migrate(target="head").to_dict()
    finally:
        engine.dispose()
    keep = ("success", "rolled_back", "error")
    out = {k: rep.get(k) for k in keep}
    for side in ("before", "after"):
        state = rep.get(side) or {}
        out[side] = {"alembic_revision": state.get("alembic_revision"), "om_meta": state.get("om_meta")}
    return out


def direct_runner_report(url: str, module_id: str) -> dict:
    """A MigrationRunner handed an engine directly (no schema selection: check_module_connection)."""
    from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS
    from openminion.modules.storage.migrations.runner import MigrationRunner

    engine = sa.create_engine(url)
    try:
        runner = MigrationRunner(module_id=module_id, db_path=":memory:",
                                 module_application_id=MODULE_APPLICATION_IDS.get(module_id, 0),
                                 backend_type="postgres", engine=engine)
        rep = runner.migrate(target="head").to_dict()
    finally:
        engine.dispose()
    return {k: rep.get(k) for k in ("success", "rolled_back", "error")}


def hook_competitor(url: str, schema: str) -> dict:
    """Just before the first plain `CREATE SCHEMA "<schema>"` this process runs, another
    connection creates and commits that schema (a competitor that won the race)."""
    state = {"fired": False}

    def before(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        s = " ".join(str(statement).split())
        if not state["fired"] and s.startswith(f'CREATE SCHEMA "{schema}"'):
            state["fired"] = True
            comp = sa.create_engine(url, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
            try:
                with comp.connect() as c:
                    c.execute(text(f'CREATE SCHEMA "{schema}"'))
            finally:
                comp.dispose()
            state["statement"] = s

    sa.event.listen(sa.engine.Engine, "before_cursor_execute", before)
    return state


def run_child_action(action: str, args: list[str]) -> dict:
    quiet()
    if action == "start_runtime":
        ok, msg = L.attempt(lambda: L.start_runtime(args[0], Path(args[1])))
        return {"ok": ok, "message": msg}
    if action == "runtime_rows":
        ok, msg = L.attempt(lambda: L.runtime_rows(args[0]))
        return {"ok": ok, "message": msg}
    if action == "legacy_module":
        ok, msg = L.attempt(lambda: L.legacy_module(args[0], args[1]))
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
    if action == "runner_report":
        try:
            return {"ok": True, "report": runner_report(args[0], args[1])}
        except BaseException as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {' '.join(str(exc).split())}"}
    if action == "runner_report_hooked":
        state = hook_competitor(args[0], args[2])
        try:
            rep = runner_report(args[0], args[1])
            return {"ok": True, "report": rep, "hook": state}
        except BaseException as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {' '.join(str(exc).split())}", "hook": state}
    if action == "cli_hooked":
        # The installed console script's entry point, in this process, with the competitor hook.
        state = hook_competitor(args[0], args[2])
        from openminion.cli.main import entrypoint

        sys.argv = ["openminion", "storage", "migrate", "--backend", "postgres", "--postgres-url", args[0],
                    "--module", args[1], "--json"]
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf):
            try:
                rc = entrypoint()
                code = int(rc or 0)
            except SystemExit as exc:
                code = int(exc.code or 0) if not isinstance(exc.code, str) else 1
        return {"ok": code == 0, "exit": code, "stdout": buf.getvalue()[-4000:], "hook": state}
    return {"ok": False, "message": f"unknown action {action}"}


def child(python: str, action: str, *args: str, root: Path) -> dict:
    return L.child(python, PROBE, action, *[str(a) for a in args], root=root)


def spawn(python: str, action: str, *args: str, root: Path) -> subprocess.Popen:
    root.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([python, "-I", "-X", "utf8", str(PROBE), "--child", action, *[str(a) for a in args]],
                            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def child_result(proc: subprocess.Popen, timeout: int = 600) -> dict:
    out, err = proc.communicate(timeout=timeout)
    for line in reversed(out.splitlines()):
        if line.startswith("CHILD_RESULT "):
            return json.loads(line[len("CHILD_RESULT "):])
    return {"ok": False, "message": f"child exit {proc.returncode}: {err[-800:]}"}


# ---------------------------------------------------------------------------- helpers

def call(fn) -> tuple[dict, str | None]:
    """(value, None) or ({}, the error text)."""
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001
        return {}, f"{type(exc).__name__}: {' '.join(str(exc).split())}"


def ledgers_and_counts(snap: dict) -> dict:
    return {s: {"ledgers": b["ledgers"], "rows": {t: i["rows"] for t, i in b["tables"].items()},
                "columns": {t: i["columns"] for t, i in b["tables"].items()}} for s, b in snap.items()}


def runtime_ok(result: tuple[bool, str]) -> bool:
    return result == (True, str(HEAD))


def engine_tables_of(url: str, schema: str = "public") -> dict:
    sigs = L.signatures(url, schema)
    return {t: sigs[t] for t in sorted(L.ENGINE_TABLES & set(sigs))}


def only_new_empty_schema(before: dict, after: dict, schema: str) -> bool:
    """`after` is `before` plus one empty `schema` (the competitor's) and nothing else."""
    if schema in before or schema not in after:
        return False
    rest = {k: v for k, v in after.items() if k != schema}
    block = after[schema]
    return rest == before and not block["tables"] and not block["indexes"] and not block["sequences"]


class Blocker:
    """A competitor's uncommitted `CREATE SCHEMA`, committed once another backend waits on it."""

    def __init__(self, url: str, schema: str) -> None:
        self.url, self.schema = url, schema
        self.engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
        self.conn = self.engine.connect()
        self.pid = int(self.conn.execute(text("SELECT pg_backend_pid()")).scalar())
        self.conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    def wait_blocked(self, seconds: float = 60.0) -> list[int]:
        end = time.monotonic() + seconds
        eng = L.admin()
        try:
            while time.monotonic() < end:
                with eng.connect() as c:
                    pids = list(c.execute(text(
                        "SELECT pid FROM pg_stat_activity WHERE datname = :d AND :b = ANY(pg_blocking_pids(pid))"),
                        {"d": L.dbname(self.url), "b": self.pid}).scalars())
                if pids:
                    return pids
                time.sleep(0.1)
        finally:
            eng.dispose()
        return []

    def commit(self) -> None:
        self.conn.commit()
        self.close()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.close()
        self.engine.dispose()


# ---------------------------------------------------------------------------- engine

def engine_0(c: Checks, work: Path) -> None:
    """The 0.1.17 FAIL: an arbitrary-shaped engine table in the base schema."""
    per = {}
    for table in sorted(L.ENGINE_TABLES):
        url = L.new_db(f"e_arbitrary_{table[:12]}")
        L.execute(url, f"CREATE TABLE public.{table} (foreign_payload text)",
                  f"INSERT INTO public.{table} VALUES ('someone else')")
        before = L.snapshot(url)
        start = L.attempt(lambda url=url, table=table: L.start_runtime(url, work / f"e0-{table}"))
        after = L.snapshot(url)
        conns = L.wait_connections_zero(url)
        sec = L.migrate(url, work / f"e0-{table}", "secret")
        after_sec = L.snapshot(url)
        L.execute(url, f"DROP TABLE public.{table}")
        later = L.attempt(lambda url=url, table=table: L.start_runtime(url, work / f"e0-{table}-later"))
        per[table] = {"start": [start[0], L.short(start[1], 450)], "diff": L.diff_snap(before, after),
                      "connections": conns, "secret_exit": sec["exit"],
                      "public_unchanged_by_secret": after_sec.get("public") == before.get("public"),
                      "secret_schema": "public__secret" in after_sec, "later": list(later)}
    observe("engine.arbitrary_in_base", per)
    c.check("engine: an arbitrary-shaped core_events, core_sidecar_rows or sidecar_ingest_log (one column, one row) "
            "in the base schema: runtime storage's start is refused with \"Nothing was written\", the table named "
            "and \"other columns than this code's storage engine creates\"; the full snapshot (columns, rows, "
            "indexes, ledgers of every schema) is identical before and after; no connection left",
            all(not v["start"][0] and L.NOTHING in v["start"][1] and t in v["start"][1]
                and OTHER_COLUMNS in v["start"][1] and not v["diff"] and v["connections"] == 0
                for t, v in per.items()),
            {t: {"start": v["start"], "diff": v["diff"][:8], "connections": v["connections"]} for t, v in per.items()})
    c.check("engine: with that table present, `storage migrate --module secret` still proceeds into public__secret "
            "and leaves public unchanged; once the foreign table is removed, runtime storage starts at its head",
            all(v["secret_exit"] == 0 and v["secret_schema"] and v["public_unchanged_by_secret"]
                and v["later"] == [True, str(HEAD)] for v in per.values()),
            {t: {"secret_exit": v["secret_exit"], "public_unchanged": v["public_unchanged_by_secret"],
                 "later": v["later"]} for t, v in per.items()})


def engine_1(c: Checks, work: Path) -> None:
    """Unowned engine tables: this code's own shapes, without a runtime storage ledger."""
    url = L.new_db("e_unowned")
    L.attempt(lambda: L.start_runtime(url, work / "e1-build"))
    keep = L.ENGINE_TABLES & set(L.tables(url, "public"))
    drop = [t for t in L.tables(url, "public") if t not in keep]
    L.execute(url, *[f'DROP TABLE public."{t}" CASCADE' for t in drop])
    shapes = engine_tables_of(url)
    listed = {t: s in H.engine_tables.get(t, set()) for t, s in shapes.items()}
    before = L.snapshot(url)
    start = L.attempt(lambda: L.start_runtime(url, work / "e1"))
    after = L.snapshot(url)
    conns = L.wait_connections_zero(url)
    c.check("engine: the three engine tables with exactly this code's listed shapes but no runtime storage ledger "
            "(`migrations`) in the base schema: runtime storage's start is refused (\"matches no recorded runtime "
            "storage ledger\", \"Nothing was written\"); snapshot identical; no connection left",
            sorted(shapes) == sorted(L.ENGINE_TABLES) and all(listed.values()) and not start[0]
            and "matches no recorded runtime storage ledger" in start[1] and L.NOTHING in start[1]
            and before == after and conns == 0,
            {"tables": sorted(shapes), "listed_shapes": listed, "start": [start[0], L.short(start[1], 450)],
             "diff": L.diff_snap(before, after), "connections": conns})


def engine_2(c: Checks, work: Path) -> None:
    """A proven runtime history whose engine table has another shape."""
    url = L.new_db("e_proven_altered")
    first = L.attempt(lambda: L.start_runtime(url, work / "e2-first"))
    L.runtime_rows(url)
    L.execute(url, "ALTER TABLE public.core_events ADD COLUMN foreign_extra text")
    before = L.snapshot(url)
    start = L.attempt(lambda: L.start_runtime(url, work / "e2"))
    after = L.snapshot(url)
    conns = L.wait_connections_zero(url)
    L.execute(url, "ALTER TABLE public.core_events DROP COLUMN foreign_extra")
    mid = L.snapshot(url)
    later = L.attempt(lambda: L.start_runtime(url, work / "e2-later"))
    end = L.snapshot(url)
    c.check("engine: a proven runtime history (this code's first start, with rows) whose core_events gained a "
            "column: the next start is refused (\"holds core_events with other columns than this code's storage "
            "engine creates\", \"Nothing was written\"), snapshot identical, no connection left; with the column "
            "dropped it starts again, applies nothing and every ledger and row is as before",
            runtime_ok(first) and not start[0] and "core_events" in start[1] and OTHER_COLUMNS in start[1]
            and L.NOTHING in start[1] and before == after and conns == 0 and runtime_ok(later)
            and mid["public"]["ledgers"] == end["public"]["ledgers"]
            and all(mid["public"]["tables"][t]["rows"] == end["public"]["tables"][t]["rows"]
                    for t in mid["public"]["tables"]),
            {"first": first, "start": [start[0], L.short(start[1], 400)], "diff": L.diff_snap(before, after),
             "later": later, "connections": conns})


def engine_3(c: Checks, work: Path) -> None:
    """In-place module selection: a dedicated registry database (0.1.16's `storage migrate --module
    registry` alone) plus a foreign core_events in that base schema."""
    results = {}
    for variant in ("control", "foreign"):
        url = L.new_db(f"e_inplace_{variant}")
        built = L.migrate(url, work / f"e3-{variant}-build", "registry", python=LEGACY)
        L.add_rows(url, "public")
        if variant == "foreign":
            L.execute(url, "CREATE TABLE public.core_events (foreign_payload text)",
                      "INSERT INTO public.core_events VALUES ('someone else')")
        before = L.snapshot(url, ignore_migrated_at=True)
        mig = L.migrate(url, work / f"e3-{variant}", "registry")
        mid = L.snapshot(url, ignore_migrated_at=True)
        store = L.attempt(lambda url=url: L.open_close("registry_store", url, work / f"e3-{variant}-store"))
        direct_rep, direct_err = call(lambda url=url: direct_runner_report(url, "registry"))
        after = L.snapshot(url, ignore_migrated_at=True)
        conns = L.wait_connections_zero(url)
        results[variant] = {"built": built["exit"], "migrate": [mig["exit"], L.short(L.entry_error(mig, "registry"), 400)],
                            "store": [store[0], L.short(store[1], 400)], "direct": direct_rep or {"raised": direct_err},
                            "diff_migrate": L.diff_snap(before, mid), "diff_all": L.diff_snap(before, after),
                            "public__registry": "public__registry" in after, "connections": conns,
                            "ledger_after": after["public"]["ledgers"]}
    observe("engine.in_place_registry", results)
    ctl, bad = results["control"], results["foreign"]
    c.check("engine: control: on a dedicated registry database written by 0.1.16 (`storage migrate --module "
            "registry` alone, rows added), the installed `storage migrate --module registry`, the registry store "
            "and a MigrationRunner handed the engine directly all use the base schema in place (no "
            "public__registry)",
            ctl["built"] == 0 and ctl["migrate"][0] == 0 and ctl["store"][0] and ctl["direct"].get("success") is True
            and not ctl["public__registry"],
            {k: ctl[k] for k in ("built", "migrate", "store", "direct", "public__registry")})
    c.check("engine: the same database with an arbitrary-shaped core_events in the base schema: `storage migrate "
            "--module registry` exits 1, the registry store and the directly handed MigrationRunner (check_module_"
            "connection) are refused, each with \"core_events with other columns than this code's storage engine "
            "creates\" and \"Nothing was written\"; snapshot identical (om_meta.last_migrated_at aside), no "
            "public__registry, no connection left",
            bad["migrate"][0] == 1 and OTHER_COLUMNS in bad["migrate"][1] and L.NOTHING in bad["migrate"][1]
            and not bad["store"][0] and OTHER_COLUMNS in bad["store"][1] and L.NOTHING in bad["store"][1]
            and bad["direct"].get("success") is False and OTHER_COLUMNS in str(bad["direct"].get("error"))
            and L.NOTHING in str(bad["direct"].get("error"))
            and not bad["diff_all"] and not bad["public__registry"] and bad["connections"] == 0,
            {k: bad[k] for k in ("migrate", "store", "direct", "diff_all", "public__registry", "connections")})


# ---------------------------------------------------------------------------- collision

def collide_store(c: Checks, work: Path, store: str) -> dict:
    module = L.store_module(store)
    schema = f"public__{module}"
    url = L.new_db(f"c_store_{module}")
    before = L.snapshot(url)
    blocker = Blocker(url, schema)
    try:
        proc = spawn(sys.executable, "open_store", store, url, work / f"c-{store}", root=work / f"c-{store}-cwd")
        waiting = blocker.wait_blocked()
        blocker.commit()
    finally:
        blocker.close()
    res = child_result(proc)
    after = L.snapshot(url)
    conns = L.wait_connections_zero(url)
    later = L.attempt(lambda: L.open_close(store, url, work / f"c-{store}-later"))
    listed = H.match(module, url, schema)
    others = sorted(set(L.schemas(url)) - {"public", schema}) + L.tables(url, "public")
    return {"waited_on_competitor": bool(waiting), "result": [res.get("ok"), L.short(res.get("message", ""), 450)],
            "only_competitor_schema": only_new_empty_schema(before, after, schema), "diff": L.diff_snap(before, after),
            "connections": conns, "later": [later[0], L.short(later[1], 200)], "listed": listed,
            "elsewhere": others}


def collision_0(c: Checks, work: Path) -> None:
    per = {st: collide_store(c, work, st) for st in ("memory_store", "registry_store")}
    observe("collision.store_23505", per)
    c.check("collision: a module store's first start whose CREATE SCHEMA waits on a competitor's uncommitted "
            "CREATE SCHEMA of the same name (memory store, registry store), the competitor then committing "
            "(PostgreSQL raises 23505 on pg_namespace_nspname_index): the start raises SchemaOwnershipError "
            "\"... is being initialized by another concurrent process ... Nothing was written\"; the database is "
            "the starting one plus only the competitor's empty schema; no connection left",
            all(v["waited_on_competitor"] and v["result"][0] is False
                and v["result"][1].startswith("SchemaOwnershipError:") and COMPETING in v["result"][1]
                and L.NOTHING in v["result"][1] and v["only_competitor_schema"] and v["connections"] == 0
                for v in per.values()),
            {k: {x: v[x] for x in ("waited_on_competitor", "result", "diff", "connections")} for k, v in per.items()})
    c.check("collision: after it, a later start of the same store succeeds in that schema, which then holds one "
            "listed current history; nothing was created anywhere else",
            all(v["later"][0] and v["listed"] and not v["elsewhere"] for v in per.values()),
            {k: {x: v[x] for x in ("later", "listed", "elsewhere")} for k, v in per.items()})


def collision_1(c: Checks, work: Path) -> None:
    """The migration path, 23505: `storage migrate` (JSON and text) and the runner's report."""
    out = {}
    for mode in ("cli_json", "cli_text", "report"):
        url = L.new_db(f"c_mig_{mode}")
        before = L.snapshot(url)
        blocker = Blocker(url, "public__secret")
        try:
            cwd = work / f"c-mig-{mode}"
            cwd.mkdir(parents=True, exist_ok=True)
            if mode == "report":
                proc = spawn(sys.executable, "runner_report", url, "secret", root=cwd)
            else:
                argv = [L.console(), "storage", "migrate", "--backend", "postgres", "--postgres-url", url,
                        "--module", "secret"] + (["--json"] if mode == "cli_json" else [])
                proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            waiting = blocker.wait_blocked()
            blocker.commit()
        finally:
            blocker.close()
        if mode == "report":
            res = child_result(proc)
        else:
            stdout, stderr = proc.communicate(timeout=600)
            res = {"exit": proc.returncode, "stdout": stdout[-3000:], "stderr": stderr[-1500:]}
            if mode == "cli_json":
                try:
                    res["entry"] = json.loads(stdout)["modules"][0]
                except Exception:  # noqa: BLE001
                    res["entry"] = None
        after = L.snapshot(url)
        conns = L.wait_connections_zero(url)
        later = L.migrate(url, work / f"c-mig-{mode}-later", "secret")
        out[mode] = {"waited_on_competitor": bool(waiting), "result": res,
                     "only_competitor_schema": only_new_empty_schema(before, after, "public__secret"),
                     "diff": L.diff_snap(before, after), "connections": conns, "later_exit": later["exit"],
                     "listed": H.match("secret", url, "public__secret")}
    observe("collision.migrate_23505", out)
    j, t, r = out["cli_json"], out["cli_text"], out["report"]
    err_j = " ".join(str((j["result"].get("entry") or {}).get("error", "")).split())
    rep = (r["result"].get("report") or {})
    c.check("collision: `storage migrate --module secret --json` whose CREATE SCHEMA waits on a competitor's "
            "uncommitted CREATE SCHEMA, the competitor then committing: exit 1, the entry is `error` with \"... is "
            "being initialized by another concurrent process ... Nothing was written\"; in text the line is "
            "`  FAIL  secret: ...` with the same message; stderr empty",
            j["waited_on_competitor"] and t["waited_on_competitor"] and j["result"]["exit"] == 1
            and COMPETING in err_j and L.NOTHING in err_j and t["result"]["exit"] == 1
            and any(line.startswith("  FAIL  secret: ") and COMPETING in line and L.NOTHING in line
                    for line in t["result"]["stdout"].splitlines())
            and not j["result"]["stderr"].strip() and not t["result"]["stderr"].strip(),
            {"json_exit": j["result"]["exit"], "json_error": L.short(err_j, 400),
             "text_exit": t["result"]["exit"], "text_stdout": L.short(t["result"]["stdout"], 400),
             "stderr": [L.short(j["result"]["stderr"], 300), L.short(t["result"]["stderr"], 300)]})
    c.check("collision: the MigrationRunner `storage migrate` builds, in the same collision, returns a failed "
            "report with rolled_back=True, the competing-start error with \"Nothing was written\", and before/"
            "after states with no alembic revision and no om_meta, which is what the server holds: the starting "
            "database plus only the competitor's empty public__secret; no connection left after any of the three",
            r["waited_on_competitor"] and rep.get("success") is False and rep.get("rolled_back") is True
            and COMPETING in str(rep.get("error")) and L.NOTHING in str(rep.get("error"))
            and (rep.get("before") or {}).get("alembic_revision") is None
            and not (rep.get("before") or {}).get("om_meta")
            and (rep.get("after") or {}).get("alembic_revision") is None and not (rep.get("after") or {}).get("om_meta")
            and all(v["only_competitor_schema"] and v["connections"] == 0 for v in out.values()),
            {"report": rep, "child": {k: v for k, v in r["result"].items() if k != "report"},
             "diffs": {k: v["diff"] for k, v in out.items()}, "connections": {k: v["connections"] for k, v in out.items()}})
    c.check("collision: after each, a later `storage migrate --module secret` exits 0 in public__secret, which "
            "holds one listed current history",
            all(v["later_exit"] == 0 and v["listed"] for v in out.values()),
            {k: [v["later_exit"], v["listed"]] for k, v in out.items()})


def collision_2(c: Checks, work: Path) -> None:
    """The migration path, 42P06: the competitor commits just before the migration's CREATE SCHEMA."""
    out = {}
    for mode in ("report", "cli"):
        url = L.new_db(f"c_dup_{mode}")
        before = L.snapshot(url)
        res = child(sys.executable, "runner_report_hooked" if mode == "report" else "cli_hooked", url, "secret",
                    "public__secret", root=work / f"c-dup-{mode}")
        after = L.snapshot(url)
        conns = L.wait_connections_zero(url)
        later = L.migrate(url, work / f"c-dup-{mode}-later", "secret")
        out[mode] = {"result": res, "only_competitor_schema": only_new_empty_schema(before, after, "public__secret"),
                     "diff": L.diff_snap(before, after), "connections": conns, "later_exit": later["exit"],
                     "listed": H.match("secret", url, "public__secret")}
    observe("collision.migrate_42P06", out)
    rep = out["report"]["result"].get("report") or {}
    cli = out["cli"]["result"]
    try:
        entry = json.loads(cli.get("stdout", ""))["modules"][0]
    except Exception:  # noqa: BLE001
        entry = {}
    err = " ".join(str(entry.get("error", "")).split())
    c.check("collision: a competitor that creates and commits public__secret just before the migration's own "
            "CREATE SCHEMA (PostgreSQL raises 42P06 DuplicateSchema): the runner's report is failed with "
            "rolled_back=True and the competing-start error with \"Nothing was written\"; the console script's "
            "entry point (same hook) exits 1 with that error in JSON; the server holds the starting database plus "
            "only the competitor's empty schema; no connection left",
            bool((out["report"]["result"].get("hook") or {}).get("fired")) and rep.get("success") is False
            and rep.get("rolled_back") is True and COMPETING in str(rep.get("error"))
            and L.NOTHING in str(rep.get("error")) and bool((cli.get("hook") or {}).get("fired"))
            and cli.get("exit") == 1 and COMPETING in err and L.NOTHING in err
            and all(v["only_competitor_schema"] and v["connections"] == 0 for v in out.values()),
            {"report": rep, "report_hook": out["report"]["result"].get("hook"), "cli_exit": cli.get("exit"),
             "cli_error": L.short(err, 400), "cli_hook": cli.get("hook"),
             "diffs": {k: v["diff"] for k, v in out.items()}})
    c.check("collision (42P06): a later `storage migrate --module secret` exits 0 into a listed current history",
            all(v["later_exit"] == 0 and v["listed"] for v in out.values()),
            {k: [v["later_exit"], v["listed"]] for k, v in out.items()})


def collision_3(c: Checks, work: Path) -> None:
    """Free-running concurrent first starts, as in the 0.1.17 verification (6 rounds)."""
    outcomes: dict = {"registry_store": [], "memory_store": [], "storage migrate --module secret": []}
    later_ok, layouts_ok, others = [], [], []
    for rnd in range(6):
        u = L.new_db(f"c_race_{rnd}")
        start_at = time.time() + 2.5
        procs = []
        for st in ("registry_store",) * 3 + ("memory_store",) * 3:
            procs.append((st, spawn(sys.executable, "concurrent_store", st, u,
                                    work / f"race{rnd}-{st}-{uuid.uuid4().hex[:4]}", start_at,
                                    root=work / f"race{rnd}-cwd")))
        for _ in range(3):
            procs.append(("storage migrate --module secret", subprocess.Popen(
                [L.console(), "storage", "migrate", "--backend", "postgres", "--postgres-url", u, "--module", "secret",
                 "--json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(work))))
        for kind, p in procs:
            if kind.startswith("storage"):
                out, err = p.communicate(timeout=600)
                try:
                    e = json.loads(out)["modules"][0]
                    res = {"ok": p.returncode == 0, "message": " ".join(str(e.get("error", "")).split())}
                except Exception:  # noqa: BLE001
                    res = {"ok": False, "message": f"exit {p.returncode}: {(out + err)[-400:]}"}
            else:
                res = child_result(p)
            outcomes[kind].append(res)
        later = {st: L.attempt(lambda st=st: L.open_close(st, u, work / f"race{rnd}-later-{st}"))[0]
                 for st in ("registry_store", "memory_store")}
        later["secret"] = L.migrate(u, work / f"race{rnd}-later", "secret")["exit"] == 0
        later_ok.append(all(later.values()))
        layouts_ok.append(all(H.match(m, u, f"public__{m}") for m in ("registry", "memory", "secret")))
        others.append(sorted(set(L.schemas(u)) - {"public", "public__registry", "public__memory", "public__secret"})
                      + L.tables(u, "public"))
    summary = {k: {"ok": sum(1 for r in v if r["ok"]), "failed": [L.short(r["message"], 260) for r in v if not r["ok"]]}
               for k, v in outcomes.items()}
    failures = [r["message"] for v in outcomes.values() for r in v if not r["ok"]]
    kinds = {"competing start (Nothing was written)": sum(1 for m in failures if COMPETING in m and L.NOTHING in m),
             "other refusal with Nothing was written": sum(1 for m in failures if COMPETING not in m and L.NOTHING in m),
             "UniqueViolation without the refusal": sum(1 for m in failures if "UniqueViolation" in m and L.NOTHING not in m),
             "DuplicateSchema without the refusal": sum(1 for m in failures if "DuplicateSchema" in m and L.NOTHING not in m),
             "other": [L.short(m, 200) for m in failures if L.NOTHING not in m and "UniqueViolation" not in m
                       and "DuplicateSchema" not in m],
             "attempts": sum(len(v) for v in outcomes.values())}
    observe("collision.free_running", {"outcomes": summary, "kinds": kinds})
    c.check("collision: free-running concurrent first starts (6 rounds, each a new database: 3 registry stores, 3 "
            "memory stores and 3 `storage migrate --module secret` started together): every failure is a refusal "
            "with \"Nothing was written\" (no raw UniqueViolation or DuplicateSchema)",
            all(L.NOTHING in m for m in failures), {"kinds": kinds, "outcomes": summary})
    c.check("collision: in every round a later start of each store and a later migrate succeed, each module schema "
            "holds one listed current history, and nothing was created elsewhere",
            all(later_ok) and all(layouts_ok) and not any(others),
            {"later": later_ok, "layouts": layouts_ok, "elsewhere": others})


# ---------------------------------------------------------------------------- report

def report_0(c: Checks, work: Path) -> None:
    """A refusal before the migration transaction begins: rolled_back=False, nothing written."""
    url = L.new_db("r_prebegin")
    L.migrate(url, work / "r0-build", "registry", python=LEGACY)
    L.execute(url, "CREATE TABLE public.core_events (foreign_payload text)")
    before = L.snapshot(url, ignore_migrated_at=True)
    rep, err = call(lambda: runner_report(url, "registry"))
    after = L.snapshot(url, ignore_migrated_at=True)
    c.check("report: a refusal before the migration transaction (the in-place registry with a foreign core_events) "
            "returns a failed report with rolled_back=False (nothing was begun) and the refusal with \"Nothing was "
            "written\"; the database is unchanged",
            rep.get("success") is False and rep.get("rolled_back") is False and L.NOTHING in str(rep.get("error"))
            and OTHER_COLUMNS in str(rep.get("error")) and before == after,
            {"report": rep, "raised": err, "diff": L.diff_snap(before, after)})


def report_1(c: Checks, work: Path) -> None:
    """Runtime storage's error and `storage migrate`'s report for the same foreign base table agree."""
    url = L.new_db("r_runtime_vs_report")
    L.execute(url, "CREATE TABLE public.core_events (foreign_payload text)",
              "INSERT INTO public.core_events VALUES ('someone else')")
    before = L.snapshot(url)
    rt = L.attempt(lambda: L.start_runtime(url, work / "r1"))
    plan = L.migrate(url, work / "r1", plan=True)
    after = L.snapshot(url)
    c.check("report: runtime storage's start on a base schema with a foreign core_events raises (type recorded) "
            "with \"Nothing was written\", and the database is in fact unchanged; `storage migrate --plan --json` "
            "on the same URL reports the modules' own schemas and writes nothing either",
            not rt[0] and L.NOTHING in rt[1] and before == after and plan["exit"] in (0, 1),
            {"runtime": L.short(rt[1], 450), "plan_exit": plan["exit"],
             "plan_entries": {k: v.get("status") for k, v in plan["entries"].items()},
             "diff": L.diff_snap(before, after)})


# ---------------------------------------------------------------------------- histories

def histories_0(c: Checks, work: Path) -> None:
    """Runtime databases written by 0.1.16 and by 0.1.23: accepted in place, nothing restamped."""
    per = {}
    for label, py in (("0.1.16", LEGACY), ("0.1.23", PREV)):
        if not py:
            per[label] = {"skipped": "no interpreter"}
            continue
        url = L.new_db(f"h_runtime_{label.replace('.', '')}")
        built = child(py, "start_runtime", url, work / f"h0-{label}", root=work / f"h0-{label}-cwd")
        rows = child(py, "runtime_rows", url, root=work / f"h0-{label}-cwd")
        pre = L.snapshot(url)
        eng_pre = engine_tables_of(url)
        first = L.attempt(lambda url=url, label=label: L.start_runtime(url, work / f"h0-{label}-new"))
        mid = L.snapshot(url)
        second = L.attempt(lambda url=url, label=label: L.start_runtime(url, work / f"h0-{label}-new2"))
        end = L.snapshot(url)
        old_rows = pre["public"]["ledgers"].get("migrations") or []
        new_rows = mid["public"]["ledgers"].get("migrations") or []
        per[label] = {"built": built, "rows": rows, "first": list(first), "second": list(second),
                      "migrations_before": len(old_rows), "migrations_after": len(new_rows),
                      "earlier_rows_kept": new_rows[:len(old_rows)] == old_rows,
                      "engine_tables_unchanged": engine_tables_of(url) == eng_pre and bool(eng_pre),
                      "engine_shapes_listed": all(s in H.engine_tables.get(t, set()) for t, s in eng_pre.items()),
                      "sessions_rows": [pre["public"]["tables"]["sessions"]["rows"],
                                        end["public"]["tables"]["sessions"]["rows"]],
                      "second_changed_nothing": ledgers_and_counts(mid) == ledgers_and_counts(end), "listed": H.match("runtime", url, "public")}
    observe("histories.runtime", per)
    c.check("histories: runtime databases written by the published 0.1.16 and 0.1.23 (their first start, with "
            "rows; their engine tables have listed shapes): the installed runtime storage starts in place at its "
            "head, keeps every earlier `migrations` row and every row, leaves the engine tables' definitions "
            "unchanged, ends at a listed current history; a second start changes no ledger, table definition or row "
            "count",
            all(v.get("built", {}).get("ok") and runtime_ok(tuple(v["first"])) and runtime_ok(tuple(v["second"]))
                and v["earlier_rows_kept"] and v["engine_tables_unchanged"] and v["engine_shapes_listed"]
                and v["sessions_rows"][0] == v["sessions_rows"][1] == 1 and v["second_changed_nothing"]
                and v["listed"] for v in per.values()),
            per)


def histories_1(c: Checks, work: Path) -> None:
    """A full 0.1.23 database (runtime first, then `storage migrate`, rows everywhere) upgraded in place."""
    if not PREV:
        c.check("histories: full 0.1.23 database", False, "NOT RUN: no --prev-python")
        return
    url = L.new_db("h_full_0123")
    built_rt = child(PREV, "start_runtime", url, work / "h1-build", root=work / "h1-cwd")
    built_mig = L.migrate(url, work / "h1-build", python=PREV)
    stores = {st: child(PREV, "open_store", st, url, work / f"h1-build-{st}", root=work / "h1-cwd")
              for st in ("memory_store", "registry_store", "a2a_audit_store")}
    for schema in L.schemas(url):
        L.add_rows(url, schema)
    pre = L.snapshot(url)
    mig = L.migrate(url, work / "h1")
    rt = L.attempt(lambda: L.start_runtime(url, work / "h1-rt"))
    opened = {st: L.attempt(lambda st=st: L.open_close(st, url, work / f"h1-{st}"))
              for st in ("memory_store", "registry_store", "a2a_audit_store")}
    post = L.snapshot(url)
    lost = []
    for schema, block in pre.items():
        for t, info in block["tables"].items():
            if t in L.LEDGER_TABLES or t == "migrations":
                continue
            now = (post.get(schema, {}).get("tables", {}).get(t) or {}).get("rows")
            if now is None or now < info["rows"]:
                lost.append(f"{schema}.{t}: {info['rows']} -> {now}")
    module_schemas = sorted(s for s in post if s.startswith("public__"))
    unlisted = [s for s in module_schemas if not H.match(s.split("__", 1)[1], url, s)]
    c.check("histories: a full database written by the published 0.1.23 (runtime storage first, then `storage "
            "migrate --backend postgres`, the memory, registry and A2A audit stores, a row in every table): the "
            "installed `storage migrate --backend postgres` exits 0, runtime storage starts at its head in place, "
            "the three stores open; no row is lost; runtime storage and every module schema end at a listed "
            "current history; no schema was added or moved",
            built_rt.get("ok") and built_mig["exit"] == 0 and all(v.get("ok") for v in stores.values())
            and mig["exit"] == 0 and runtime_ok(rt) and all(v[0] for v in opened.values()) and not lost
            and not unlisted and sorted(pre) == sorted(post) and bool(H.match("runtime", url, "public")),
            {"built": [built_rt, built_mig["exit"], {k: v.get("ok") for k, v in stores.items()}],
             "migrate_exit": mig["exit"], "migrate_errors": {k: v.get("error") for k, v in mig["entries"].items()
                                                             if v.get("status") == "error"},
             "runtime": rt, "stores": {k: [v[0], L.short(v[1], 200)] for k, v in opened.items()}, "lost": lost,
             "unlisted": unlisted, "schemas_before": sorted(pre), "schemas_after": sorted(post)})


def histories_2(c: Checks, work: Path, prev_histories: str | None = None) -> None:
    """The listed-history file: every 0.1.23 history is still listed."""
    if not PREV:
        c.check("histories: listed file", False, "NOT RUN: no --prev-python")
        return
    path = subprocess.run([PREV, "-I", "-c", "import openminion, pathlib; print(pathlib.Path(openminion.__file__)"
                           ".parent / 'modules/storage/migrations/postgres_histories.json')"],
                          capture_output=True, text=True).stdout.strip()
    old = L.Histories(path)
    key = lambda h: (h["owner"], h["ledger"], json.dumps(h["tables"], sort_keys=True))  # noqa: E731
    new_keys = {key(h) for h in H.items}
    missing = [f"{h['owner']} {h['ledger']}" for h in old.items if key(h) not in new_keys]
    engine_kept = all(old.engine_tables[t] <= H.engine_tables.get(t, set()) for t in old.engine_tables)
    c.check("histories: every history the published 0.1.23 lists (owner, ledger, table signatures) is still listed "
            "by the installed package, and every 0.1.23 engine-table shape is still listed",
            not missing and engine_kept,
            {"0.1.23": {"sha256": old.sha256, "histories": len(old.items)},
             "installed": {"sha256": H.sha256, "histories": len(H.items)}, "missing": missing,
             "engine_shapes_kept": engine_kept})


# ---------------------------------------------------------------------------- neighbors

def neighbors_0(c: Checks, work: Path) -> None:
    cases = {}
    # Foreign engine table in the module's own schema (refused on 0.1.17 already).
    u = L.new_db("n_core_events_module")
    L.execute(u, 'CREATE SCHEMA "public__registry"', 'CREATE TABLE "public__registry".core_events (foreign_payload text)',
              "INSERT INTO \"public__registry\".core_events VALUES ('someone else')")
    b = L.snapshot(u)
    r1 = L.migrate(u, work / "n0a", "registry")
    r2 = L.attempt(lambda: L.open_close("registry_store", u, work / "n0a-store"))
    cases["core_events in public__registry"] = (
        r1["exit"] == 1 and not r2[0] and "records no registry ledger" in L.entry_error(r1, "registry")
        and L.NOTHING in r2[1] and L.snapshot(u) == b, [L.short(L.entry_error(r1, "registry"), 200), L.short(r2[1], 200)])
    # Another application's sessions and customers.
    u = L.new_db("n_foreign_sessions")
    L.execute(u, "CREATE TABLE public.sessions (id int PRIMARY KEY, owner text)", "INSERT INTO public.sessions VALUES (1, 'x')",
              "CREATE TABLE public.customers (id int PRIMARY KEY)", "INSERT INTO public.customers VALUES (1)")
    b = L.snapshot(u)
    r = L.attempt(lambda: L.start_runtime(u, work / "n0b"))
    cases["foreign sessions"] = (not r[0] and "sessions" in r[1] and L.NOTHING in r[1] and L.snapshot(u) == b,
                                 L.short(r[1], 250))
    # An unrelated table only: runtime storage starts and leaves it alone.
    u = L.new_db("n_unrelated")
    L.execute(u, "CREATE TABLE public.customers (id int PRIMARY KEY, name text)", "INSERT INTO public.customers VALUES (1, 'a')")
    b = L.snapshot(u)["public"]["tables"]["customers"]
    r = L.attempt(lambda: L.start_runtime(u, work / "n0c"))
    cases["unrelated table"] = (runtime_ok(r) and L.snapshot(u)["public"]["tables"]["customers"] == b, list(r))
    # An empty database.
    u = L.new_db("n_empty")
    r = L.attempt(lambda: L.start_runtime(u, work / "n0d"))
    again = L.attempt(lambda: L.start_runtime(u, work / "n0d2"))
    cases["empty database, twice"] = (runtime_ok(r) and runtime_ok(again) and bool(H.match("runtime", u, "public")),
                                      [list(r), list(again)])
    observe("neighbors.layouts", {k: v[1] for k, v in cases.items()})
    c.check("neighbors: a foreign core_events in public__registry is still refused (`storage migrate` exit 1, the "
            "store refused, nothing written); another application's `sessions` is still refused for runtime "
            "storage; an unrelated table is left alone while runtime storage starts; an empty database starts "
            "twice at a listed current history",
            all(v[0] for v in cases.values()), {k: [v[0], v[1]] for k, v in cases.items()})


def neighbors_1(c: Checks, work: Path) -> None:
    """Role without CREATE / USAGE (from the 0.1.17 verification, head read from the package)."""
    u = L.new_db("n_role")
    role = f"om446_app_{uuid.uuid4().hex[:6]}"
    eng = L.admin()
    with eng.connect() as conn:
        conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'om446'"))
    eng.dispose()
    try:
        L.execute(u, f'GRANT CONNECT ON DATABASE "{L.dbname(u)}" TO {role}',
                  f'CREATE SCHEMA "public__memory" AUTHORIZATION {role}', 'CREATE SCHEMA "public__secret"',
                  'CREATE TABLE "public__secret".alembic_version (version_num varchar(32) PRIMARY KEY)')
        role_url = sa.engine.make_url(u).set(username=role, password="om446").render_as_string(hide_password=False)
        before = L.snapshot(u)
        mem = L.attempt(lambda: L.open_close("memory_store", role_url, work / "role-mem"))
        no_grant = L.entry_error(L.migrate(role_url, work / "role", "secret"), "secret")
        no_create = L.entry_error(L.migrate(role_url, work / "role", "registry"), "registry")
        after = L.snapshot(u)
        conns = L.wait_connections_zero(u)
        head = L.module_head("memory")
        c.check("neighbors: permissions: a role without CREATE uses the empty public__memory a DBA created for it; "
                f"the refusals give `GRANT USAGE, CREATE ON SCHEMA \"public__secret\" TO \"{role}\"` and `CREATE "
                f"SCHEMA \"public__registry\" AUTHORIZATION \"{role}\"`, and write nothing; no connection left",
                mem[0] and after.get("public__memory", {}).get("ledgers", {}).get("alembic_version") == [head]
                and f'GRANT USAGE, CREATE ON SCHEMA "public__secret" TO "{role}"' in no_grant
                and f'CREATE SCHEMA "public__registry" AUTHORIZATION "{role}"' in no_create
                and after["public__secret"] == before["public__secret"] and "public__registry" not in after
                and conns == 0,
                {"memory": list(mem), "head": head, "no_grant": L.short(no_grant, 300),
                 "no_create": L.short(no_create, 300), "connections": conns})
    finally:
        with contextlib.suppress(Exception):
            L.execute(u, f"DROP OWNED BY {role}")
        eng = L.admin()
        with eng.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {L.qi(L.dbname(u))} WITH (FORCE)"))
            conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        eng.dispose()
        if L.dbname(u) in L.CREATED:
            L.CREATED.remove(L.dbname(u))


def neighbors_2(c: Checks, work: Path) -> None:
    """An injected failure inside a store's first start rolls the new schema back (0.1.17 item 4)."""
    url = L.new_db("n_store_failure")
    L.execute(url,
              "CREATE FUNCTION public.probe_446_fail() RETURNS event_trigger LANGUAGE plpgsql AS $$ BEGIN "
              "IF EXISTS (SELECT 1 FROM pg_event_trigger_ddl_commands() WHERE object_identity LIKE "
              "'%memory_records%' OR object_identity LIKE '%audit_records%') THEN RAISE EXCEPTION "
              "'probe 446: injected failure'; END IF; END $$",
              "CREATE EVENT TRIGGER probe_446_fail ON ddl_command_end EXECUTE FUNCTION public.probe_446_fail()")
    before = L.snapshot(url)
    res = {st: L.attempt(lambda st=st: L.open_close(st, url, work / f"n2-{st}")) for st in ("memory_store", "a2a_audit_store")}
    mig = L.migrate(url, work / "n2", "memory")
    after = L.snapshot(url)
    conns = L.wait_connections_zero(url)
    L.execute(url, "DROP EVENT TRIGGER probe_446_fail")
    ok_after = {st: L.attempt(lambda st=st: L.open_close(st, url, work / f"n2b-{st}"))[0]
                for st in ("memory_store", "a2a_audit_store")}
    c.check("neighbors: rollback: the memory store, the A2A audit store and `storage migrate --module memory` each "
            "fail on an injected DDL error; no public__memory or public__a2a is left, nothing else changed, no "
            "connection left; with the trigger dropped both stores start",
            not res["memory_store"][0] and not res["a2a_audit_store"][0] and mig["exit"] == 1
            and all("probe 446: injected failure" in m for _, m in res.values())
            and "probe 446: injected failure" in L.entry_error(mig, "memory") and before == after and conns == 0
            and all(ok_after.values()),
            {"stores": {k: L.short(m, 200) for k, (_, m) in res.items()}, "migrate": L.short(L.entry_error(mig, "memory"), 200),
             "diff": L.diff_snap(before, after), "connections": conns, "after_drop": ok_after})


SECTIONS = ("engine", "collision", "report", "histories", "neighbors")


def parts(name: str) -> list:
    found = sorted((int(k.rsplit("_", 1)[1]), v) for k, v in globals().items()
                   if k.startswith(name + "_") and k.rsplit("_", 1)[1].isdigit() and callable(v))
    return [f for _, f in found]


def main() -> int:
    global LEGACY, PREV, H, HEAD
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("out")
    ap.add_argument("--legacy-python")
    ap.add_argument("--prev-python")
    ap.add_argument("--only", default=",".join(SECTIONS))
    a = ap.parse_args()
    quiet()
    LEGACY, PREV = a.legacy_python, a.prev_python
    import hashlib

    import openminion

    pkg = Path(openminion.__file__).parent
    H = L.Histories(pkg / "modules/storage/migrations/postgres_histories.json")
    HEAD = max(H.runtime_versions)
    schema_py = (pkg / "modules/storage/migrations/postgres_schema.py").read_bytes()
    blob = hashlib.sha1(b"blob %d\0" % len(schema_py) + schema_py).hexdigest()
    versions = {}
    for label, py in (("legacy", LEGACY), ("prev", PREV)):
        if py:
            versions[label] = subprocess.run([py, "-I", "-c", "import openminion;print(openminion.__version__)"],
                                             capture_output=True, text=True).stdout.strip()
    info = banner("probe_446_0124", server=L.server_version(), histories_sha256=H.sha256, runtime_head=HEAD,
                  postgres_schema_py_git_blob=blob, builders=versions)
    print("builders:", versions, "| runtime head:", HEAD, "| postgres_schema.py blob:", blob, flush=True)
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
               "commands": L.RUNS, "databases_dropped": dropped}
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
