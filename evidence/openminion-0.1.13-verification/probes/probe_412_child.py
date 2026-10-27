"""Child process of probe_412.py: one storagectl command, or the seed, in a
process whose cyclic garbage collector is off before openminion is imported.

    python -I -X utf8 probe_412_child.py cli <result.json> <target_db> -- <storagectl args...>
    python -I -X utf8 probe_412_child.py seed <result.json> <db>

Both modes call gc.disable() first and replace gc.collect with a counter that
does not collect (the count must stay 0), so only explicit close() and
reference counting can release a sqlite3 connection. sqlite3.connect is
wrapped to remember where each connection was opened (ids only: the wrapper
keeps no reference that could hold a connection open).

cli: installs a recording progress reporter in place of storagectl's
select_default_reporter (as upstream's test_storagectl_restore.py does) and
wraps pathlib.Path.replace so that, when the target database is replaced, it
lists the sqlite3 connections still open on the target (gc.get_objects(),
which does not collect) and the size of the target's -wal file, then calls
the real replace. It then runs the installed storagectl entry point exactly as
`python -m openminion.modules.storage` does (runpy). Once the command has
returned or raised, still in the same process with GC still off, it lists the
connections left open on the target and renames, replaces and deletes the
target file (each undone afterwards; the bytes are checked unchanged).
Everything the probe needs goes to <result.json>; stdout, stderr and the
exit code are the command's own (an exception that escapes is printed and
gives exit code 1, as the interpreter does).

seed: writes two sessions with turns, events and working state through the
installed SQLiteSessionStore, closes it, then adds a probe_marker table whose
rows cover text, integer, real, blob and NULL values.
"""

from __future__ import annotations

import gc

gc.disable()

GC_CALLS = {"n": 0}


def _counting_collect(*_args, **_kwargs):
    GC_CALLS["n"] += 1
    return 0


gc.collect = _counting_collect

import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import runpy  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import traceback  # noqa: E402
from contextlib import closing  # noqa: E402

OPENED: dict[int, dict] = {}
_CONNECT = sqlite3.connect


def _site() -> str:
    frames = []
    for frame in traceback.extract_stack()[:-2]:
        name = frame.filename.replace("\\", "/")
        index = name.rfind("/openminion/")
        if index >= 0:
            frames.append(f"{name[index + 1:]}:{frame.lineno} {frame.name}")
    return " <- ".join(reversed(frames[-6:])) or "(probe code)"


def _tracking_connect(*args, **kwargs):
    conn = _CONNECT(*args, **kwargs)
    database = args[0] if args else kwargs.get("database")
    OPENED[id(conn)] = {"site": _site(), "database": str(database)}
    return conn


sqlite3.connect = _tracking_connect  # type: ignore[assignment]
sqlite3.dbapi2.connect = _tracking_connect  # type: ignore[assignment]


def same(a, b) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def err(exc: BaseException) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return f"{type(exc).__name__} [WinError {winerror}]"
    errno = getattr(exc, "errno", None)
    if errno is not None:
        return f"{type(exc).__name__} [errno {errno}]"
    return f"{type(exc).__name__}: {exc}"


def open_connections(path) -> dict:
    """sqlite3 connections still open on `path`, without collecting."""
    holders, uninspectable = [], 0
    for obj in gc.get_objects():
        if not isinstance(obj, sqlite3.Connection):
            continue
        try:
            files = [row[2] for row in obj.execute("PRAGMA database_list") if row[2]]
        except sqlite3.ProgrammingError:
            continue  # closed
        except Exception:  # noqa: BLE001 - e.g. a connection owned by another thread
            uninspectable += 1
            continue
        if any(same(name, path) for name in files):
            holders.append(OPENED.get(id(obj), {}).get("site", "(opened before tracking)"))
    return {"count": len(holders), "opened at": holders, "connections that could not be inspected": uninspectable}


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files_next_to(path: pathlib.Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir() if p.is_file()) if path.parent.exists() else []


def handle_operations(target: pathlib.Path) -> dict:
    """Rename, replace and delete the target right away; undo each one."""
    ops: dict[str, str] = {}
    moved = target.with_name(target.name + ".probe-moved")
    try:
        os.rename(target, moved)
        ops["rename the target away"] = "ok"
    except OSError as exc:
        ops["rename the target away"] = err(exc)
    if moved.exists():
        try:
            os.rename(moved, target)
            ops["rename it back"] = "ok"
        except OSError as exc:
            ops["rename it back"] = err(exc)
    staged = target.with_name(target.name + ".probe-copy")
    try:
        shutil.copy2(target, staged)
        os.replace(staged, target)
        ops["replace the target with a byte copy (os.replace)"] = "ok"
    except OSError as exc:
        ops["replace the target with a byte copy (os.replace)"] = err(exc)
    finally:
        if staged.exists():
            staged.unlink()
    keep = target.with_name(target.name + ".probe-keep")
    try:
        shutil.copy2(target, keep)
        os.remove(target)
        ops["delete the target"] = "ok"
    except OSError as exc:
        ops["delete the target"] = err(exc)
    finally:
        if keep.exists():
            if target.exists():
                keep.unlink()
            else:
                os.replace(keep, target)
                ops["put the deleted target back"] = "ok"
    return ops


def run_cli(result_path: pathlib.Path, target: pathlib.Path, cli_args: list[str]) -> int:
    from openminion.modules.storage import cli as storage_cli

    calls: list[list] = []
    selected = {"n": 0}

    class RecordingReporter:
        def on_start(self, *, total=None, label=""):
            calls.append(["start", label])

        def on_progress(self, *, advance=1, message=None):
            calls.append(["progress", advance])

        def on_end(self, *, success, message=None):
            calls.append(["end", success])

    def select_default_reporter(*_args, **_kwargs):
        selected["n"] += 1
        return RecordingReporter()

    storage_cli.select_default_reporter = select_default_reporter

    replacements: list[dict] = []
    real_replace = pathlib.Path.replace

    def tracking_replace(self, dest):
        if not same(dest, target):
            return real_replace(self, dest)
        wal = pathlib.Path(str(dest) + "-wal")
        entry = {
            "source": pathlib.Path(self).name,
            "open connections on the target at the replacement": open_connections(dest),
            "target -wal bytes at the replacement": wal.stat().st_size if wal.exists() else None,
        }
        replacements.append(entry)
        try:
            result = real_replace(self, dest)
        except OSError as exc:
            entry["replace"] = err(exc)
            raise
        entry["replace"] = "ok"
        return result

    pathlib.Path.replace = tracking_replace  # type: ignore[method-assign]

    exit_code, exception = 0, None
    sys.argv = ["storagectl", *cli_args]
    try:
        runpy.run_module("openminion.modules.storage", run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException as exc:  # noqa: BLE001 - reported like the interpreter does
        exit_code = 1
        exception = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        pathlib.Path.replace = real_replace  # type: ignore[method-assign]
    sys.stdout.flush()
    sys.stderr.flush()

    after: dict = {
        "open connections on the target": open_connections(target),
        "connections opened on the target during the command": [
            record["site"] for record in OPENED.values() if record["database"] not in ("", ":memory:") and same(record["database"], target)
        ],
        "target exists": target.exists(),
        "files next to the target": files_next_to(target),
    }
    if target.exists():
        after["target sha256 before the operations"] = sha256(target)
        after["operations right after the command"] = handle_operations(target)
        after["target sha256 after the operations"] = sha256(target)
        after["files next to the target after the operations"] = files_next_to(target)
    payload = {
        "mode": "cli",
        "argv": cli_args,
        "exit_code": exit_code,
        "exception": exception,
        "progress reporter selections": selected["n"],
        "progress calls": calls,
        "replacements of the target": replacements,
        "after the command": after,
        "gc": {"enabled": gc.isenabled(), "gc.collect calls": GC_CALLS["n"]},
    }
    result_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return exit_code  # the process exits with the command's code, as `python -m` would


def run_seed(result_path: pathlib.Path, db: pathlib.Path) -> int:
    from openminion.modules.session.storage.store import SQLiteSessionStore

    store = SQLiteSessionStore(db)
    try:
        first = store.create_session(session_id="probe412-a", title="probe 412 session A",
                                     tags=["probe412"], initial_agent_id="openminion")
        store.append_turn(first, "user", "hello before the backup")
        store.append_turn(first, "assistant", "hello")
        store.append_event(first, "probe.note", {"n": 1, "text": "written before the backup"})
        store.put_working_state(first, state_inline={"step": 1})
        second = store.create_session(session_id="probe412-b", title="probe 412 session B")
        store.append_turn(second, "user", "a second session")
    finally:
        store.close()
    with closing(_CONNECT(str(db))) as conn, conn:
        conn.execute("CREATE TABLE probe_marker(id INTEGER PRIMARY KEY, v TEXT, n INTEGER, r REAL, b BLOB, z)")
        conn.executemany(
            "INSERT INTO probe_marker(id, v, n, r, b, z) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "before-backup", 412, 0.5, bytes(range(256)), None),
                (2, "row two", -1, 1e-300, b"", "text in an untyped column"),
                (3, "row three", 2**62, -2.25, b"\x00\x01", 7),
            ],
        )
    with closing(_CONNECT(str(db))) as conn:
        counts = {
            name: conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        }
    payload = {
        "mode": "seed",
        "rows per table after the seed": {k: v for k, v in counts.items() if v},
        "open connections on the database after the seed": open_connections(db),
        "gc": {"enabled": gc.isenabled(), "gc.collect calls": GC_CALLS["n"]},
    }
    result_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return 0


def main() -> int:
    mode, result_path, target = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
    if mode == "seed":
        return run_seed(result_path, target)
    if mode == "cli":
        if sys.argv[4] != "--":
            raise SystemExit("usage: probe_412_child.py cli <result.json> <target_db> -- <storagectl args...>")
        return run_cli(result_path, target, sys.argv[5:])
    raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    sys.exit(main())
