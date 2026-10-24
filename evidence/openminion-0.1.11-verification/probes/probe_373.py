"""#373: migration-owned SQLite connections close without garbage collection.

Automatic cyclic GC is disabled before openminion is imported, and gc.collect()
is never called (it is wrapped to count calls; the count must stay 0). There are
no cleanup retries anywhere.

  A. Fresh migration: MigrationRunner(...).migrate(target="head") on a new, empty
     file for 14 modules. Every sqlite3 connection opened during migrate() is
     recorded with a strong reference and must be closed when migrate() returns.
     The public metadata step must have committed om_meta (schema_head,
     last_migrated_at). A second fresh file per module, with no tracking, is
     renamed and deleted right after migrate() returns.
  B. Detection/verification: detect() and verify(level="quick") on a module DB
     (the same shape as the PR's tests) and on a migrated DB; each opens one
     connection and closes it.
  C. Metadata write: _update_sqlite_metadata_after_migration() commits om_meta
     and closes its connection.
  D. Failing metadata hook: _upsert_sqlite_om_meta writes a row, then raises.
     Direct call: the error propagates, the write is rolled back, the
     connection is closed. Through migrate(): the report carries the error, the
     snapshot is restored (rolled_back), all connections are closed, and the
     file can be renamed.
  E. Failing verification hook: a verifier_hook writes a row, then raises.
     Owned (runner.verify / run_verification with db_path) and through
     migrate(): the error propagates, the write is rolled back, the owned
     connection is closed. Borrowed (run_verification(connection=...)): the
     error propagates, the caller's connection stays open and its transaction
     stays with the caller (the write is still pending until the caller rolls
     back). The hook writes to its own table, verify_probe_rows.
  F. Native Windows SDK turn with authored-tool storage (no tracking): an echo
     runtime on a temporary root runs one turn, runtime.close(), then
     immediately os.replace() + os.remove() of authored_tools/store.sqlite and
     shutil.rmtree() of the whole runtime root. 5 fresh roots, sequentially.
  G. Attribution for F (separate runtime, strong tracking): every connection
     opened by the migration runner/verification owners during runtime
     construction is closed after runtime.close(). Any other connection still
     open is recorded as an observation with its opening site (not a #373 check).

Usage: python -I probe_373.py <work_dir> <out.json>
"""

from __future__ import annotations

import gc

gc.disable()
_GC_COLLECT = gc.collect
GC_COLLECT_CALLS: list[str] = []


def _counting_collect(*args, **kwargs):  # pragma: no cover - must never run
    import traceback

    GC_COLLECT_CALLS.append("".join(traceback.format_stack(limit=6)))
    return _GC_COLLECT(*args, **kwargs)


gc.collect = _counting_collect

import os  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
from contextlib import closing  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, SqliteTracker, banner, clear_override_env, echo_runtime, quiet, sanitize_text, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)
quiet()
clear_override_env()
info = banner("probe_373", gc_enabled_at_start=gc.isenabled())

from openminion import Agent  # noqa: E402
from openminion.modules.storage.migrations import MigrationRunner  # noqa: E402
from openminion.modules.storage.migrations.module_ids import get_module_application_id  # noqa: E402
from openminion.modules.storage.migrations.verify import run_verification  # noqa: E402


c = Checks()
obs: dict[str, object] = {}
OWNER_SITES = ("runner.py", "verify.py")
MODULES = [
    "secret", "policy", "authoring", "session", "identity", "registry", "telemetry",
    "task", "skill", "memory", "artifact", "retrieve", "commerce", "controlplane",
]
MODULE_APP_ID = 0x4F4D0001


def om_meta(path: Path) -> dict[str, str]:
    with closing(sqlite3.connect(str(path))) as conn:
        return dict(conn.execute("SELECT key, value FROM om_meta").fetchall())


def init_module_db(path: Path) -> None:
    """A module DB at revision 0001_init, as in the PR's tests (_init_db)."""
    with closing(sqlite3.connect(str(path))) as conn, conn:
        conn.execute(f"PRAGMA application_id = {MODULE_APP_ID}")
        conn.execute("PRAGMA user_version = 1")
        conn.execute("CREATE TABLE IF NOT EXISTS alembic_version(version_num TEXT PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO alembic_version(version_num) VALUES ('0001_init')")
        conn.execute("CREATE TABLE IF NOT EXISTS om_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO om_meta(key, value) VALUES ('module_id', 'sessctl')")


def module_runner(path: Path, **kwargs) -> MigrationRunner:
    init_module_db(path)
    return MigrationRunner(
        module_id="sessctl", db_path=path, module_application_id=MODULE_APP_ID,
        snapshot_root=path.parent / f"{path.stem}-snapshots", **kwargs,
    )


def rename_and_delete(path: Path) -> str:
    moved = path.with_name(path.name + ".moved")
    try:
        os.replace(path, moved)
    except OSError as exc:
        return f"rename failed: {type(exc).__name__} [WinError {getattr(exc, 'winerror', None)}]"
    try:
        os.remove(moved)
    except OSError as exc:
        return f"delete failed: {type(exc).__name__} [WinError {getattr(exc, 'winerror', None)}]"
    return "ok"


# ------------------------------------------------------------------ A
a_rows = []
for module in MODULES:
    db = WORK / "A" / f"{module}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.touch()
    runner = MigrationRunner(module_id=module, db_path=db, module_application_id=get_module_application_id(module))
    with SqliteTracker(strong=True) as t:
        report = runner.migrate(target="head")
    status = t.status()
    meta = om_meta(db)
    a_rows.append({
        "module": module,
        "success": report.success,
        "head": report.after.alembic_revision,
        "connections_opened": len(status),
        "connections_open_after_return": sum(1 for s in status if not s["closed"]),
        "sites": sorted({s["site"] for s in status}),
        "om_meta_schema_head": meta.get("schema_head"),
        "om_meta_has_last_migrated_at": bool(meta.get("last_migrated_at")),
    })
    t.drop()
    # Separate fresh file, no tracking at all: rename/delete right after migrate().
    db2 = WORK / "A-untracked" / f"{module}.db"
    db2.parent.mkdir(parents=True, exist_ok=True)
    db2.touch()
    r2 = MigrationRunner(module_id=module, db_path=db2, module_application_id=get_module_application_id(module)).migrate(target="head")
    a_rows[-1]["untracked_success"] = r2.success
    a_rows[-1]["untracked_rename_delete_right_after_migrate"] = rename_and_delete(db2)
obs["A_fresh_migration"] = a_rows
c.check("A: migrate(target='head') succeeds on a fresh file for all 14 modules",
        all(r["success"] and r["untracked_success"] for r in a_rows), [(r["module"], r["head"]) for r in a_rows])
c.check("A: every connection opened during migrate() is closed when it returns (no gc.collect)",
        all(r["connections_opened"] > 0 and r["connections_open_after_return"] == 0 for r in a_rows),
        {r["module"]: f"{r['connections_opened']} opened, {r['connections_open_after_return']} open" for r in a_rows})
obs["A_opening_sites"] = sorted({s for r in a_rows for s in r["sites"]})  # informational: detect, backup snapshot, alembic upgrade, metadata, verify
c.check("A: the public metadata step committed om_meta schema_head = head and last_migrated_at",
        all(r["om_meta_schema_head"] == r["head"] and r["om_meta_has_last_migrated_at"] for r in a_rows))
c.check("A: each untracked fresh DB is renamed and deleted right after migrate() returns, GC disabled",
        all(r["untracked_rename_delete_right_after_migrate"] == "ok" for r in a_rows),
        sorted({r["untracked_rename_delete_right_after_migrate"] for r in a_rows}))

# ------------------------------------------------------------------ B
(WORK / "B").mkdir()
runner_b = module_runner(WORK / "B" / "module.db")
with SqliteTracker(strong=True) as t:
    state = runner_b.detect()
    after_detect = len(t.status())
    verification = runner_b.verify(level="quick")
    st = t.status()
obs["B_detect_verify_module_db"] = {"application_id": state.application_id, "revision": state.alembic_revision,
                                     "verification_ok": verification.ok, "connections": st, "opened_by_detect": after_detect}
c.check("B: detect() opens one connection and verify() one more; both closed on return",
        state.application_id == MODULE_APP_ID and verification.ok and after_detect == 1 and len(st) == 2
        and all(s["closed"] for s in st), [(s["site"], s["closed"]) for s in st])
t.drop()
migrated = WORK / "A" / "secret.db"
mr = MigrationRunner(module_id="secret", db_path=migrated, module_application_id=get_module_application_id("secret"))
with SqliteTracker(strong=True) as t:
    st_state = mr.detect()
    st_ver = mr.verify(level="full")
    st = t.status()
obs["B_detect_verify_migrated_db"] = {"revision": st_state.alembic_revision, "verification_ok": st_ver.ok,
                                       "integrity_check": st_ver.integrity_check, "connections": st}
c.check("B: detect() + verify(level='full') on a migrated DB close both connections",
        st_ver.ok and len(st) == 2 and all(s["closed"] for s in st), [(s["site"], s["closed"]) for s in st])
t.drop()
c.check("B: the migrated DB can be renamed and deleted right after detect/verify", rename_and_delete(migrated) == "ok")

# ------------------------------------------------------------------ C
(WORK / "C").mkdir()
runner_c = module_runner(WORK / "C" / "module.db")
with SqliteTracker(strong=True) as t:
    runner_c._update_sqlite_metadata_after_migration(target="head", migrated_at="2026-10-24T00:00:00+00:00")
    st = t.status()
meta_c = om_meta(WORK / "C" / "module.db")
obs["C_metadata_commit"] = {"om_meta": meta_c, "connections": st}
c.check("C: a successful metadata write is committed (read back on a new connection)",
        meta_c.get("schema_head") == "0001_init" and meta_c.get("last_migrated_at") == "2026-10-24T00:00:00+00:00", meta_c)
c.check("C: the metadata connection is closed on return", len(st) == 1 and st[0]["closed"], st)
t.drop()

# ------------------------------------------------------------------ D
(WORK / "D").mkdir()
runner_d = module_runner(WORK / "D" / "module.db")


def failing_upsert(*, connection: sqlite3.Connection, values: dict[str, str]) -> None:
    del values
    connection.execute("INSERT INTO om_meta(key, value) VALUES ('probe', 'written')")
    raise RuntimeError("forced metadata failure")


runner_d._upsert_sqlite_om_meta = failing_upsert  # type: ignore[method-assign]
with SqliteTracker(strong=True) as t:
    try:
        runner_d._update_sqlite_metadata_after_migration(target="head", migrated_at="2026-10-24T00:00:00+00:00")
        raised = None
    except RuntimeError as exc:
        raised = str(exc)
    st = t.status()
meta_d = om_meta(WORK / "D" / "module.db")
obs["D_metadata_failure_direct"] = {"raised": raised, "om_meta": meta_d, "connections": st}
c.check("D: a failing metadata hook propagates its error", raised == "forced metadata failure", raised)
c.check("D: the failed metadata write is rolled back", "probe" not in meta_d and "schema_head" not in meta_d, meta_d)
c.check("D: the owned metadata connection is closed after the failure", len(st) == 1 and st[0]["closed"], st)
t.drop()

# Same hook through the public migrate() on a fresh secret DB.
db_dm = WORK / "D" / "secret.db"
db_dm.touch()
runner_dm = MigrationRunner(module_id="secret", db_path=db_dm, module_application_id=get_module_application_id("secret"),
                            snapshot_root=WORK / "D" / "secret-snapshots")
runner_dm._upsert_sqlite_om_meta = failing_upsert  # type: ignore[method-assign]
with SqliteTracker(strong=True) as t:
    try:
        rep = runner_dm.migrate(target="head")
        rep_view = {"success": rep.success, "error": rep.error, "rolled_back": rep.rolled_back}
    except Exception as exc:  # noqa: BLE001 - recorded as a failed check (seen on the 0.1.10 control)
        rep_view = {"raised": f"{type(exc).__name__} [WinError {getattr(exc, 'winerror', None)}]"}
    st = t.status()
with closing(sqlite3.connect(str(db_dm))) as conn:
    tables = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
obs["D_metadata_failure_through_migrate"] = {
    **rep_view, "tables_after": tables, "connections_opened": len(st), "open_after": [s for s in st if not s["closed"]],
}
t.drop()
c.check("D: through migrate(), the report carries the metadata error and the snapshot is restored",
        rep_view == {"success": False, "error": "forced metadata failure", "rolled_back": True} and "om_meta" not in tables,
        {**rep_view, "tables_after": tables})
c.check("D: through migrate(), every connection is closed and the file renames/deletes right away",
        len(st) > 0 and all(s["closed"] for s in st) and rename_and_delete(db_dm) == "ok", f"{len(st)} opened")

# ------------------------------------------------------------------ E


def failing_verifier(conn) -> list:
    conn.execute("INSERT INTO verify_probe_rows(note) VALUES ('written by the hook')")
    raise RuntimeError("forced verification failure")


def add_probe_table(path: Path) -> None:
    with closing(sqlite3.connect(str(path))) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS verify_probe_rows(note TEXT)")


def probe_rows(path: Path) -> int:
    with closing(sqlite3.connect(str(path))) as conn:
        return conn.execute("SELECT count(*) FROM verify_probe_rows").fetchone()[0]


(WORK / "E").mkdir()
runner_e = module_runner(WORK / "E" / "module.db", verifier_hook=failing_verifier)
add_probe_table(WORK / "E" / "module.db")
with SqliteTracker(strong=True) as t:
    try:
        runner_e.verify(level="quick")
        raised = None
    except RuntimeError as exc:
        raised = str(exc)
    st = t.status()
meta_e = probe_rows(WORK / "E" / "module.db")
obs["E_verifier_failure_owned"] = {"raised": raised, "hook_rows_after": meta_e, "connections": st}
c.check("E: owned verification (runner.verify) propagates the hook error", raised == "forced verification failure", raised)
c.check("E: owned verification rolls back the hook's write", meta_e == 0, meta_e)
c.check("E: the owned verification connection is closed after the failure", len(st) == 1 and st[0]["closed"], st)
t.drop()

# Through migrate(): the pre-migration verify raises, so migrate() propagates it.
db_em = WORK / "E" / "secret.db"
add_probe_table(db_em)  # the only table in an otherwise fresh file (application_id 0)
runner_em = MigrationRunner(module_id="secret", db_path=db_em, module_application_id=get_module_application_id("secret"),
                            snapshot_root=WORK / "E" / "secret-snapshots", verifier_hook=lambda conn: failing_verifier(conn))
with SqliteTracker(strong=True) as t:
    try:
        runner_em.migrate(target="head")
        raised_m = None
    except RuntimeError as exc:
        raised_m = str(exc)
    except Exception as exc:  # noqa: BLE001 - recorded as a failed check
        raised_m = f"{type(exc).__name__} [WinError {getattr(exc, 'winerror', None)}]"
    st = t.status()
with closing(sqlite3.connect(str(db_em))) as conn:
    tables_em = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
rows_em = probe_rows(db_em)
obs["E_verifier_failure_through_migrate"] = {"raised": raised_m, "tables_after": tables_em, "hook_rows_after": rows_em,
                                              "connections": [(s["site"], s["closed"]) for s in st]}
c.check("E: through migrate(), the verification hook error propagates, its write is rolled back and nothing is migrated",
        raised_m == "forced verification failure" and rows_em == 0 and tables_em == ["verify_probe_rows"],
        {"raised": raised_m, "tables": tables_em, "hook_rows": rows_em})
c.check("E: through migrate(), every connection is closed and the file renames/deletes right away",
        len(st) > 0 and all(s["closed"] for s in st) and rename_and_delete(db_em) == "ok", f"{len(st)} opened")
t.drop()

# Borrowed connection: run_verification must leave it (and its transaction) to the caller.
db_eb = WORK / "E" / "borrowed.db"
init_module_db(db_eb)
add_probe_table(db_eb)
borrowed = sqlite3.connect(str(db_eb))
try:
    run_verification(module_id="sessctl", db_path=db_eb, level="quick", verifier_hook=failing_verifier, connection=borrowed)
    raised_b = None
except RuntimeError as exc:
    raised_b = str(exc)
borrowed_open = not SqliteTracker.is_closed(borrowed)
pending = borrowed.in_transaction
row_visible_to_caller = borrowed.execute("SELECT count(*) FROM verify_probe_rows").fetchone()[0]
borrowed.rollback()
row_after_caller_rollback = borrowed.execute("SELECT count(*) FROM verify_probe_rows").fetchone()[0]
borrowed.close()
obs["E_verifier_failure_borrowed"] = {
    "raised": raised_b, "borrowed_connection_open_after": borrowed_open, "caller_transaction_pending": pending,
    "hook_row_visible_to_caller_before_rollback": row_visible_to_caller,
    "hook_row_after_caller_rollback": row_after_caller_rollback,
}
c.check("E: borrowed connection: the hook error propagates", raised_b == "forced verification failure", raised_b)
c.check("E: borrowed connection stays open and its transaction stays with the caller",
        borrowed_open and pending and row_visible_to_caller == 1 and row_after_caller_rollback == 0,
        obs["E_verifier_failure_borrowed"])

# ------------------------------------------------------------------ F
f_rows = []
for i in range(5):
    root = WORK / "F" / f"run{i}" / "root"
    store = root / ".openminion" / "authored_tools" / "store.sqlite"
    runtime = echo_runtime(root)
    try:
        with Agent(runtime=runtime) as agent:
            run_state = agent.run("hello").run_state
    finally:
        runtime.close()
    row = {"run": i, "run_state": run_state, "store_exists": store.is_file(), "gc_enabled": gc.isenabled()}
    row["rename_delete_store"] = rename_and_delete(store)
    try:
        shutil.rmtree(root)
        row["rmtree_runtime_root"] = "ok"
    except OSError as exc:
        row["rmtree_runtime_root"] = f"{type(exc).__name__} [WinError {getattr(exc, 'winerror', None)}] {sanitize_text(str(getattr(exc, 'filename', '')))}"
    row["root_exists_after"] = root.exists()
    f_rows.append(row)
obs["F_sdk_turn_cleanup"] = f_rows
c.check("F: 5/5 echo SDK turns complete with authored-tool storage present",
        all(r["run_state"] == "completed" and r["store_exists"] for r in f_rows), [r["run_state"] for r in f_rows])
c.check("F: right after runtime.close(), store.sqlite is renamed and deleted (5/5, GC disabled, no retries)",
        all(r["rename_delete_store"] == "ok" for r in f_rows), [r["rename_delete_store"] for r in f_rows])
c.check("F: right after that, the whole temporary runtime root is removed (5/5, no retries)",
        all(r["rmtree_runtime_root"] == "ok" and not r["root_exists_after"] for r in f_rows), [r["rmtree_runtime_root"] for r in f_rows])

# ------------------------------------------------------------------ G
root_g = WORK / "G" / "root"
with SqliteTracker(strong=True) as t:
    runtime = echo_runtime(root_g)
    try:
        with Agent(runtime=runtime) as agent:
            g_state = agent.run("hello").run_state
    finally:
        runtime.close()
    st = t.status()
owner = [s for s in st if any(k in s["site"] for k in OWNER_SITES)]
others_open = [s for s in st if s not in owner and not s["closed"]]
store_targets = sorted({Path(s["target"]).name for s in owner})
obs["G_attribution"] = {
    "run_state": g_state,
    "connections_opened_total": len(st),
    "owner_connections": len(owner),
    "owner_connections_open_after_close": [s for s in owner if not s["closed"]],
    "owner_sites": sorted({s["site"] for s in owner}),
    "owner_db_files": store_targets,
    "other_connections_open_after_close": others_open,
}
c.check("G: runtime construction opens migration-runner/verification connections and all are closed after runtime.close()",
        len(owner) > 0 and not obs["G_attribution"]["owner_connections_open_after_close"],
        f"{len(owner)} owner connections over {store_targets}")
# Outside #373 (not migration-owned): any other connection still open after close, with its
# opening site. In-memory (":memory:") connections hold no file and cannot block cleanup.
obs["G_observation_other_open_connections"] = {
    "count": len(others_open),
    "all_in_memory": all(s["target"] == ":memory:" for s in others_open),
    "sites": [s["site"] for s in others_open],
}
t.drop()

c.check("gc stayed disabled and gc.collect() was never called", not gc.isenabled() and not GC_COLLECT_CALLS,
        {"gc_enabled": gc.isenabled(), "gc_collect_calls": len(GC_COLLECT_CALLS)})

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items, "observed": obs})
print("SUMMARY", c.summary())
