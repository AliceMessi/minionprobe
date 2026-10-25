"""Does a test's own `with sqlite3.connect(...) as conn:` block keep the database open?

Standard library only. Each case builds a small SQLite file in a fresh
tempfile.TemporaryDirectory() and then:

1. as in tests/test_storage_migrations.py: reads through
   `with sqlite3.connect(path) as conn:` inside the TemporaryDirectory block,
   runs gc.collect() while `conn` is still bound, and lets the block exit;
2. same, but `del conn` before the block exits, with the cyclic garbage
   collector disabled; it lists what still refers to the connection;
3. same as 2, then gc.collect() before the block exits;
4. the suggested fix: `with closing(sqlite3.connect(path)) as conn:`;
5. the restore shape from tests/storage: a write through
   `with sqlite3.connect(path) as conn:`, then os.replace() of the database
   with a copy while `conn` is still bound; and 6. the same with
   `with closing(sqlite3.connect(path)) as conn, conn:`.

Run: python probe_test_cleanup.py. The cleanup and replace results only fail
on Windows, where an open SQLite file cannot be deleted or replaced.
"""

import gc
import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing


def make_db(root):
    path = os.path.join(root, "state", "openminion.db")
    os.makedirs(os.path.dirname(path))
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE migrations(version INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO migrations VALUES (1, 'bootstrap_core_tables')")
    return path


def state(conn):
    try:
        conn.execute("SELECT 1")
        return "open"
    except sqlite3.ProgrammingError:
        return "closed"


def describe(exc):
    if getattr(exc, "winerror", None):
        return f"{type(exc).__name__} [WinError {exc.winerror}]"
    return f"{type(exc).__name__} [Errno {exc.errno}]"


def connected_to(obj, path):
    if type(obj) is not sqlite3.Connection:
        return False
    try:
        files = [row[2] for row in obj.execute("PRAGMA database_list") if row[2]]
    except sqlite3.ProgrammingError:
        return False
    target = os.path.normcase(os.path.abspath(path))
    return any(os.path.normcase(os.path.abspath(name)) == target for name in files)


def tidy(root):
    gc.collect()
    for obj in gc.get_objects():
        if type(obj) is sqlite3.Connection:
            obj.close()
    shutil.rmtree(root, ignore_errors=True)


def case_as_in_tests():
    row = {"code": "with sqlite3.connect(path) as conn: (read), inside with TemporaryDirectory()"}
    holder = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            holder["root"] = tmp
            path = make_db(tmp)
            with sqlite3.connect(path) as conn:
                conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            row["connection after the with block"] = state(conn)
            gc.collect()
            row["after gc.collect(), conn still bound"] = state(conn)
        row["TemporaryDirectory cleanup"] = "ok"
    except PermissionError as exc:
        row["TemporaryDirectory cleanup"] = describe(exc)
        tidy(holder["root"])
    return row


def case_dropped(collect):
    row = {
        "code": "same, then del conn"
        + (" and gc.collect()" if collect else ", cyclic GC disabled"),
    }
    holder = {}
    gc.disable()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            holder["root"] = tmp
            path = make_db(tmp)
            with sqlite3.connect(path) as conn:
                conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            del conn
            if collect:
                gc.collect()
            live = [obj for obj in gc.get_objects() if connected_to(obj, path)]
            row["open connections to the file"] = len(live)
            if live:
                row["referred to by"] = sorted(
                    {
                        f"{type(ref).__module__}.{type(ref).__qualname__}"
                        for item in live
                        for ref in gc.get_referrers(item)
                        if ref is not live
                    }
                )
            del live
        row["TemporaryDirectory cleanup"] = "ok"
    except PermissionError as exc:
        row["TemporaryDirectory cleanup"] = describe(exc)
        tidy(holder["root"])
    finally:
        gc.enable()
    return row


def case_closing():
    row = {"code": "with closing(sqlite3.connect(path)) as conn: (read), inside with TemporaryDirectory()"}
    holder = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            holder["root"] = tmp
            path = make_db(tmp)
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            row["connection after the with block"] = state(conn)
        row["TemporaryDirectory cleanup"] = "ok"
    except PermissionError as exc:
        row["TemporaryDirectory cleanup"] = describe(exc)
        tidy(holder["root"])
    return row


def case_replace(use_closing):
    row = {
        "code": (
            "with closing(sqlite3.connect(path)) as conn, conn: (write), then os.replace(copy, path)"
            if use_closing
            else "with sqlite3.connect(path) as conn: (write), then os.replace(copy, path)"
        )
    }
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = make_db(tmp)
        copy = path + ".restore.tmp"
        shutil.copy2(path, copy)
        if use_closing:
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("DELETE FROM migrations")
        else:
            with sqlite3.connect(path) as conn:
                conn.execute("DELETE FROM migrations")
        row["connection after the with block"] = state(conn)
        try:
            os.replace(copy, path)
            row["replace the database"] = "ok"
        except PermissionError as exc:
            row["replace the database"] = describe(exc)
        conn.close()
    return row


def main():
    report = {
        "1. as in the tests": case_as_in_tests(),
        "2. name dropped, cyclic GC disabled": case_dropped(collect=False),
        "3. name dropped, then gc.collect()": case_dropped(collect=True),
        "4. suggested fix": case_closing(),
        "5. restore shape, as in the tests": case_replace(use_closing=False),
        "6. restore shape, with closing": case_replace(use_closing=True),
    }
    print(json.dumps(report, indent=2))
    print(sys.version.split()[0], sys.platform, platform.release(), "SQLite", sqlite3.sqlite_version)


if __name__ == "__main__":
    main()
