"""#361 native Windows cleanup case for the memory audit database.

Automatic GC is disabled before openminion is imported, gc.collect() is never
called (it is wrapped to count calls) and nothing is retried.

  1. Clean runs (5 fresh runtime roots, no tracking of any kind): one echo SDK
     turn, runtime.close(), then immediately rename
     .openminion/memory/memory.audit.db, delete the renamed file, and remove the
     whole temporary runtime root with shutil.rmtree (errors are recorded, never
     retried).
  2. Attributed run (a separate root, after the clean runs): sqlite3.connect is
     wrapped to record the openminion frame that opens each connection (ids only,
     no strong references). After runtime.close(), every live sqlite3 connection
     object attached to a file under the root is listed with its opener, then the
     same rename/delete/rmtree steps run.

Usage: python -I probe_361_cleanup.py <work_dir> <out.json>
"""

from __future__ import annotations

import gc

gc.disable()
_gc_collect = gc.collect
GC_COLLECT_CALLS: list[str] = []


def _counting_collect(*args, **kwargs):  # pragma: no cover - must never run
    import traceback

    GC_COLLECT_CALLS.append(" <- ".join(f"{f.name}" for f in traceback.extract_stack()[-4:-1]))
    return _gc_collect(*args, **kwargs)


gc.collect = _counting_collect

import os  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import traceback  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, echo_runtime, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()
info = banner("probe_361_cleanup", gc_enabled_at_start=gc.isenabled())

from openminion import Agent  # noqa: E402

c = Checks()
observed: dict[str, object] = {}
AUDIT_REL = Path(".openminion") / "memory" / "memory.audit.db"


def rel(root: Path, path: str | Path) -> str:
    return os.path.relpath(str(path), str(root)).replace(os.sep, "/")


def files_under(root: Path) -> list[str]:
    out = []
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            out.append(rel(root, Path(dirpath) / name))
    return sorted(out)


def attempt(fn) -> str:
    try:
        fn()
        return "ok"
    except OSError as exc:
        win = getattr(exc, "winerror", None)
        return f"{type(exc).__name__}" + (f" [WinError {win}]" if win else "") + f": {exc.strerror}"


def cleanup(root: Path) -> dict[str, object]:
    audit = root / AUDIT_REL
    moved = audit.with_name("memory.audit.moved.db")
    result: dict[str, object] = {"audit_db_exists_after_close": audit.is_file()}
    result["rename_memory.audit.db"] = attempt(lambda: os.replace(audit, moved))
    result["delete_renamed_audit_db"] = attempt(lambda: os.remove(moved)) if moved.exists() else "skipped (rename failed)"
    blocked: list[str] = []

    def on_error(func, path, exc):  # no retry
        blocked.append(f"{func.__name__} {rel(root, path)}: {exc[1] if isinstance(exc, tuple) else exc}")

    if sys.version_info >= (3, 12):
        shutil.rmtree(root, onexc=on_error)
    else:  # pragma: no cover
        shutil.rmtree(root, onerror=on_error)
    result["rmtree_runtime_root_errors"] = blocked
    result["runtime_root_removed"] = not root.exists()
    return result


def sdk_turn(root: Path) -> str:
    runtime = echo_runtime(root)
    try:
        with Agent(runtime=runtime) as agent:
            return agent.run("hello").run_state
    finally:
        runtime.close()


# ---------------------------------------------------------------- 1. clean runs
clean = []
for i in range(5):
    root = WORK / f"clean-{i}"
    state = sdk_turn(root)
    files = files_under(root)
    row = {"run_state": state, "files_after_close": files, **cleanup(root)}
    clean.append(row)
observed["clean_runs"] = clean
summary = {
    "runs": len(clean),
    "turns_completed": sum(r["run_state"] == "completed" for r in clean),
    "audit_db_renamed": sum(r["rename_memory.audit.db"] == "ok" for r in clean),
    "audit_db_deleted": sum(r["delete_renamed_audit_db"] == "ok" for r in clean),
    "runtime_root_removed": sum(bool(r["runtime_root_removed"]) for r in clean),
}
observed["clean_summary"] = summary
c.check("1. five echo SDK turns complete", summary["turns_completed"] == 5, summary["turns_completed"])
c.check("1. memory.audit.db exists after runtime.close() in every run",
        all(r["audit_db_exists_after_close"] for r in clean), [r["audit_db_exists_after_close"] for r in clean])
c.check("1. memory.audit.db renamed immediately after runtime.close() (GC off, no retry): 5/5",
        summary["audit_db_renamed"] == 5, [r["rename_memory.audit.db"] for r in clean])
c.check("1. the renamed audit database deleted immediately: 5/5",
        summary["audit_db_deleted"] == 5, [r["delete_renamed_audit_db"] for r in clean])
c.check("1. the whole temporary runtime root removed by one shutil.rmtree, no errors: 5/5",
        summary["runtime_root_removed"] == 5 and all(not r["rmtree_runtime_root_errors"] for r in clean),
        [r["rmtree_runtime_root_errors"] for r in clean])

# ---------------------------------------------------------------- 2. attributed run
opened: dict[int, str] = {}
_connect = sqlite3.connect


def tracking_connect(*args, **kwargs):
    conn = _connect(*args, **kwargs)
    frames = [f for f in traceback.extract_stack()[:-1] if "openminion" in f.filename.replace("\\", "/")]
    opened[id(conn)] = " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames[-3:])) or "(probe)"
    return conn


sqlite3.connect = sqlite3.dbapi2.connect = tracking_connect  # type: ignore[assignment]
root = WORK / "attributed"
try:
    state = sdk_turn(root)
finally:
    sqlite3.connect = sqlite3.dbapi2.connect = _connect  # type: ignore[assignment]
norm_root = os.path.normcase(str(root))
live: list[dict[str, str]] = []
for obj in gc.get_objects():
    if type(obj) is sqlite3.Connection:
        try:
            names = [r[2] for r in obj.execute("PRAGMA database_list")]
        except sqlite3.ProgrammingError as exc:
            if "thread" in str(exc):
                live.append({"file": "(connection owned by another thread)", "opened_by": opened.get(id(obj), "?")})
            continue
        for name in names:
            if name and os.path.normcase(name).startswith(norm_root):
                live.append({"file": rel(root, name), "opened_by": opened.get(id(obj), "(opened before tracking)")})
files = files_under(root)
attributed = {"run_state": state, "connections_opened_during_run": len(opened),
              "live_connections_under_root_after_close": live, "files_after_close": files, **cleanup(root)}
observed["attributed_run"] = attributed
c.check("2. attributed run: no live sqlite3 connection to any file under the runtime root after runtime.close()",
        live == [], live)
c.check("2. attributed run: audit database renamed and deleted, runtime root removed",
        attributed["rename_memory.audit.db"] == "ok" and attributed["delete_renamed_audit_db"] == "ok"
        and attributed["runtime_root_removed"], {k: attributed[k] for k in
        ("rename_memory.audit.db", "delete_renamed_audit_db", "rmtree_runtime_root_errors", "runtime_root_removed")})

c.check("automatic GC stayed disabled and gc.collect() was never called",
        (not gc.isenabled()) and GC_COLLECT_CALLS == [], {"gc_enabled": gc.isenabled(), "gc_collect_calls": len(GC_COLLECT_CALLS)})
observed["gc"] = {"enabled_at_end": gc.isenabled(), "collect_calls": GC_COLLECT_CALLS}

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items, "observed": observed})
print(f"{c.summary()['passed']}/{c.summary()['total']} checks pass")
sys.exit(0 if c.all_pass else 1)
