"""#378: import_omx() closes every SQLite connection it opens, on success and on
failure, and leaves borrowed record stores to their owner.

Automatic cyclic GC is disabled before openminion is imported and gc.collect()
is never called (it is wrapped to count calls; the count must stay 0). No
cleanup retries.

  A. Real export/import preserves rows:
     A1. the issue's module: a `secret` DB migrated to head with 5 rows,
         export_omx(db_path=...), import_omx(target_db_path=<new file>);
     A2. a runtime-written DB: telemetry.db from one echo SDK turn (about 200
         event rows), exported and imported the same way.
     Every table's rows in the target equal the source. Every connection opened
     during import_omx() is recorded with a strong reference and must be closed
     when it returns.
  B. Forced mid-import failure into a target that already holds other rows (R0):
     B1. the PR test's failure: transfer._replace_sqlite_table_rows raises after
         writing `secrets`, the last table (alembic_version and om_meta were
         already replaced in the same transaction);
     B2. no patching: the bundle's secrets.jsonl is altered after export, so the
         checksum check raises while importing `secrets`.
     The report is unsuccessful and keeps the raised error, every table still
     holds R0 (the failed writes rolled back), and the import's connection is
     closed.
  C. Native Windows, no tracking: right after import_omx() returns, the target
     is renamed and deleted (success, B1 and B2 targets), then its directory is
     removed.
  D. Borrowed record stores: import_omx(target_record_store=...) and
     export_omx(record_store=...) with RecordStoreSQLite instances owned by the
     probe: neither calls close(), both stores stay usable, the imported rows
     are there, and the owner's close() then works.

Usage: python -I probe_378.py <work_dir> <out.json>
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
from _common import Checks, SqliteTracker, banner, clear_override_env, echo_runtime, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)
quiet()
clear_override_env()
info = banner("probe_378", gc_enabled_at_start=gc.isenabled())

from openminion import Agent  # noqa: E402
from openminion.modules.storage.migrations import MigrationRunner, export_omx, import_omx  # noqa: E402
from openminion.modules.storage.migrations import transfer  # noqa: E402
from openminion.modules.storage.migrations.module_ids import get_module_application_id  # noqa: E402
from openminion.modules.storage.record_store import RecordStoreSQLite  # noqa: E402

c = Checks()
obs: dict[str, object] = {}
SECRET_APP = get_module_application_id("secret")


def table_rows(path: Path, tables: list[str]) -> dict[str, list]:
    with closing(sqlite3.connect(str(path))) as conn:
        out = {}
        for t in tables:
            try:
                out[t] = sorted(map(tuple, conn.execute(f'SELECT * FROM "{t}"').fetchall()), key=repr)
            except sqlite3.OperationalError as exc:
                out[t] = f"OperationalError: {exc}"
        return out


def secret_db(path: Path, values: list[tuple[str, str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    assert MigrationRunner(module_id="secret", db_path=path, module_application_id=SECRET_APP).migrate(target="head").success
    with closing(sqlite3.connect(str(path))) as conn, conn:
        conn.executemany(
            "INSERT INTO secrets(key, namespace, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            [(k, ns, v, 1.0 + i, 2.0 + i) for i, (k, ns, v) in enumerate(values)],
        )
    return path


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


def rmtree_now(path: Path) -> str:
    try:
        shutil.rmtree(path)
        return "ok"
    except OSError as exc:
        return f"{type(exc).__name__} [WinError {getattr(exc, 'winerror', None)}]"


def tracked_import(**kwargs):
    with SqliteTracker(strong=True) as t:
        report = import_omx(**kwargs)
        status = t.status()
    t.drop()
    return report, status


# ------------------------------------------------------------------ A1
SRC_VALUES = [("api", "default", "s1"), ("db", "default", "s2"), ("api", "team", "s3"),
              ("token", "ci", "s4"), ("hook", "ops", "s5")]
src = secret_db(WORK / "A1" / "secret-source.db", SRC_VALUES)
manifest = export_omx(db_path=src, module_id="secret", module_application_id=SECRET_APP, export_dir=WORK / "A1" / "omx")
tables = [t.name for t in manifest.tables]
target = WORK / "A1" / "target" / "secret-import.db"
report, st = tracked_import(omx_dir=WORK / "A1" / "omx", target_db_path=target)
src_rows, tgt_rows = table_rows(src, tables), table_rows(target, tables)
obs["A1_secret"] = {
    "tables": tables, "manifest_row_counts": {t.name: t.row_count for t in manifest.tables},
    "success": report.success, "error": report.error, "exported_rows": report.exported_rows,
    "imported_rows": report.imported_rows, "target_secrets": tgt_rows.get("secrets"),
    "connections": st,
}
c.check("A1: import_omx of the secret bundle succeeds", report.success and report.error is None, report.error)
c.check("A1: every table's rows in the target equal the source (5 secrets rows)",
        src_rows == tgt_rows and len(tgt_rows["secrets"]) == 5, {t: len(v) for t, v in tgt_rows.items()})
c.check("A1: every connection import_omx opened is closed when it returns",
        len(st) >= 1 and all(s["closed"] for s in st), [(s["site"], s["closed"]) for s in st])

# ------------------------------------------------------------------ A2
root = WORK / "A2" / "root"
runtime = echo_runtime(root)
try:
    with Agent(runtime=runtime) as agent:
        a2_state = agent.run("hello").run_state
finally:
    runtime.close()
tele = root / ".openminion" / "telemetry" / "telemetry.db"
with closing(sqlite3.connect(str(tele))) as conn:
    tele_app = conn.execute("PRAGMA application_id").fetchone()[0]
m2 = export_omx(db_path=tele, module_id="telemetry", module_application_id=tele_app, export_dir=WORK / "A2" / "omx")
tables2 = [t.name for t in m2.tables]
target2 = WORK / "A2" / "target" / "telemetry-import.db"
report2, st2 = tracked_import(omx_dir=WORK / "A2" / "omx", target_db_path=target2)
rows_src2, rows_tgt2 = table_rows(tele, tables2), table_rows(target2, tables2)
obs["A2_runtime_telemetry_db"] = {
    "run_state": a2_state, "application_id": hex(tele_app),
    "manifest_row_counts": {t.name: t.row_count for t in m2.tables},
    "success": report2.success, "error": report2.error, "imported_rows": report2.imported_rows,
    "connections": st2,
}
c.check("A2: a runtime-written telemetry.db (echo SDK turn) exports and imports successfully",
        a2_state == "completed" and tele_app == get_module_application_id("telemetry") and report2.success, report2.error)
c.check("A2: every table's rows in the imported telemetry DB equal the source",
        rows_src2 == rows_tgt2 and sum(len(v) for v in rows_tgt2.values() if isinstance(v, list)) > 100,
        {t: len(v) for t, v in rows_tgt2.items()})
c.check("A2: every connection import_omx opened is closed when it returns",
        len(st2) >= 1 and all(s["closed"] for s in st2), [(s["site"], s["closed"]) for s in st2])

# ------------------------------------------------------------------ B
R0_VALUES = [("old-key", "default", "r0-a"), ("old-key-2", "legacy", "r0-b")]


def target_with_r0(name: str) -> tuple[Path, dict]:
    """A target that already holds a different, complete secret DB (R0)."""
    r0_src = secret_db(WORK / "B" / f"{name}-r0-source.db", R0_VALUES)
    export_omx(db_path=r0_src, module_id="secret", module_application_id=SECRET_APP, export_dir=WORK / "B" / f"{name}-r0-omx")
    tgt = WORK / "B" / name / "secret-import.db"
    rep = import_omx(omx_dir=WORK / "B" / f"{name}-r0-omx", target_db_path=tgt)
    assert rep.success, rep.error
    return tgt, table_rows(tgt, tables)


# B1: the PR test's forced failure while writing the last table.
tgt_b1, r0_b1 = target_with_r0("B1")
replace_rows = transfer._replace_sqlite_table_rows
written_before_failure: list[str] = []


def failing_replace(**kwargs):
    replace_rows(**kwargs)
    written_before_failure.append(kwargs["table_name"])
    if kwargs["table_name"] == "secrets":
        raise RuntimeError("forced table import failure")
    return 0


transfer._replace_sqlite_table_rows = failing_replace
try:
    rep_b1, st_b1 = tracked_import(omx_dir=WORK / "A1" / "omx", target_db_path=tgt_b1)
finally:
    transfer._replace_sqlite_table_rows = replace_rows
after_b1 = table_rows(tgt_b1, tables)
obs["B1_forced_failure"] = {"tables_written_before_failure": written_before_failure, "success": rep_b1.success,
                            "error": rep_b1.error, "rows_before": {t: len(v) for t, v in r0_b1.items()},
                            "rows_after": {t: len(v) for t, v in after_b1.items()}, "connections": st_b1}
c.check("B1: a failure while writing the last table reports success=False and keeps the error",
        rep_b1.success is False and rep_b1.error == "forced table import failure"
        and written_before_failure == ["alembic_version", "om_meta", "secrets"], {"error": rep_b1.error, "written": written_before_failure})
c.check("B1: every table still holds the target's previous rows (all writes rolled back)",
        after_b1 == r0_b1 and [r[2] for r in after_b1["secrets"]] == ["r0-a", "r0-b"], {t: len(v) for t, v in after_b1.items()})
c.check("B1: the import's connection is closed after the failure",
        len(st_b1) == 1 and st_b1[0]["closed"] and "_import_omx_tables_sqlite" in st_b1[0]["site"], st_b1)

# B2: a real checksum failure on the last table, no patching.
tgt_b2, r0_b2 = target_with_r0("B2")
omx_b2 = WORK / "B" / "B2-tampered-omx"
shutil.copytree(WORK / "A1" / "omx", omx_b2)
secrets_entry = next(t for t in manifest.tables if t.name == "secrets")
with (omx_b2 / secrets_entry.path).open("a", encoding="utf-8") as fh:
    fh.write('{"key": "injected", "namespace": "default", "value": "x", "created_at": 9.0, "updated_at": 9.0}\n')
rep_b2, st_b2 = tracked_import(omx_dir=omx_b2, target_db_path=tgt_b2)
after_b2 = table_rows(tgt_b2, tables)
obs["B2_checksum_failure"] = {"success": rep_b2.success, "error": rep_b2.error,
                              "rows_after": {t: len(v) for t, v in after_b2.items()}, "connections": st_b2}
c.check("B2: a checksum mismatch on `secrets` reports success=False and keeps the error",
        rep_b2.success is False and str(rep_b2.error).startswith("Checksum mismatch for table secrets"), rep_b2.error)
c.check("B2: every table still holds the target's previous rows", after_b2 == r0_b2, {t: len(v) for t, v in after_b2.items()})
c.check("B2: the import's connection is closed after the failure",
        len(st_b2) == 1 and st_b2[0]["closed"], [(s["site"], s["closed"]) for s in st_b2])

# ------------------------------------------------------------------ C (no tracking at all)
c_rows = {}
tgt_c = WORK / "C" / "ok" / "secret-import.db"
rep_c = import_omx(omx_dir=WORK / "A1" / "omx", target_db_path=tgt_c)
c_rows["success_import"] = {"success": rep_c.success, "rename_delete": rename_and_delete(tgt_c), "rmtree_dir": rmtree_now(tgt_c.parent)}
for name, omx in (("B1_forced_failure", None), ("B2_checksum_failure", omx_b2)):
    tgt, _ = target_with_r0(f"C-{name}")
    if omx is None:
        transfer._replace_sqlite_table_rows = failing_replace
        try:
            rep = import_omx(omx_dir=WORK / "A1" / "omx", target_db_path=tgt)
        finally:
            transfer._replace_sqlite_table_rows = replace_rows
    else:
        rep = import_omx(omx_dir=omx, target_db_path=tgt)
    c_rows[name] = {"success": rep.success, "error": rep.error, "rename_delete": rename_and_delete(tgt), "rmtree_dir": rmtree_now(tgt.parent)}
obs["C_windows_cleanup_untracked"] = c_rows
c.check("C: right after a successful import_omx, the target is renamed and deleted and its dir removed",
        c_rows["success_import"] == {"success": True, "rename_delete": "ok", "rmtree_dir": "ok"}, c_rows["success_import"])
c.check("C: right after each failed import_omx (B1, B2), the target is renamed and deleted and its dir removed",
        all(c_rows[k]["success"] is False and c_rows[k]["rename_delete"] == "ok" and c_rows[k]["rmtree_dir"] == "ok"
            for k in ("B1_forced_failure", "B2_checksum_failure")),
        {k: c_rows[k] for k in ("B1_forced_failure", "B2_checksum_failure")})

# ------------------------------------------------------------------ D (borrowed record stores)
borrowed_target = WORK / "D" / "borrowed-target.db"
borrowed_target.parent.mkdir(parents=True)
borrowed_target.touch()
assert MigrationRunner(module_id="secret", db_path=borrowed_target, module_application_id=SECRET_APP).migrate(target="head").success
close_calls: dict[int, int] = {}
orig_close = RecordStoreSQLite.close


def counting_close(self):
    close_calls[id(self)] = close_calls.get(id(self), 0) + 1
    return orig_close(self)


RecordStoreSQLite.close = counting_close
try:
    rs_target = RecordStoreSQLite(borrowed_target)
    rep_d = import_omx(omx_dir=WORK / "A1" / "omx", target_record_store=rs_target)
    target_closes = close_calls.get(id(rs_target), 0)
    usable_rows = rs_target.query_dicts("SELECT value FROM secrets ORDER BY value")
    rs_src = RecordStoreSQLite(src)
    m_d = export_omx(record_store=rs_src, module_id="secret", module_application_id=SECRET_APP, export_dir=WORK / "D" / "omx")
    src_closes = close_calls.get(id(rs_src), 0)
    src_usable = rs_src.query_dicts("SELECT count(*) AS n FROM secrets")[0]["n"]
    rs_target.close()
    rs_src.close()
    owner_closes = (close_calls.get(id(rs_target), 0), close_calls.get(id(rs_src), 0))
finally:
    RecordStoreSQLite.close = orig_close
obs["D_borrowed_record_stores"] = {
    "import_success": rep_d.success, "import_error": rep_d.error,
    "target_store_close_calls_by_import": target_closes, "target_rows_read_through_borrowed_store": [r["value"] for r in usable_rows],
    "export_tables": [t.name for t in m_d.tables], "source_store_close_calls_by_export": src_closes,
    "source_store_usable_after_export": src_usable, "close_calls_after_owner_close": owner_closes,
}
c.check("D: import_omx into a borrowed record store succeeds and does not close it",
        rep_d.success and target_closes == 0, {"success": rep_d.success, "close_calls": target_closes})
c.check("D: the borrowed target store stays usable and holds the imported rows",
        [r["value"] for r in usable_rows] == ["s1", "s2", "s3", "s4", "s5"], [r["value"] for r in usable_rows])
c.check("D: export_omx from a borrowed record store does not close it and the store stays usable",
        src_closes == 0 and src_usable == 5, {"close_calls": src_closes, "rows": src_usable})
c.check("D: the owner's own close() is the first and only close of each borrowed store", owner_closes == (1, 1), owner_closes)

c.check("gc stayed disabled and gc.collect() was never called", not gc.isenabled() and not GC_COLLECT_CALLS,
        {"gc_enabled": gc.isenabled(), "gc_collect_calls": len(GC_COLLECT_CALLS)})

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items, "observed": obs})
print("SUMMARY", c.summary())
