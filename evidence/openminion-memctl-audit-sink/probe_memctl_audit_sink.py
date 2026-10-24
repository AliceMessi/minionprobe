"""memctl write command: is the memory audit database still open after the command returns?

1. In-process, as tests/memory/test_cli_read_commands.py does: `memctl approve`
   through typer's CliRunner, with the cyclic garbage collector disabled, so
   only reference counting frees objects. After the command returns, the probe
   lists the sqlite3 connections still open on the audit database
   (`<db stem>.audit.db`) with the openminion frames that opened them, and
   tries to rename the file. It then runs the cyclic collector once with
   gc.DEBUG_SAVEALL, to list the openminion objects it finds unreachable,
   frees them, and tries to rename and delete the file again.
2. As a separate process: `python -m openminion.modules.memory approve ...`
   (the `memctl` entry point), then a rename and delete of the audit database
   right after the process exits, and the files left next to it.

Run from an openminion source checkout: PYTHONPATH="src;." python probe_memctl_audit_sink.py
(PYTHONPATH=src:. on Linux). The rename and delete results are only meaningful
on Windows, where an open SQLite file cannot be renamed or deleted.
"""

import gc
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from collections import Counter
from pathlib import Path

gc.disable()
OPENED = {}
_connect = sqlite3.connect


def _relative(filename):
    name = filename.replace("\\", "/")
    index = name.rfind("/openminion/")
    return name[index + 1:] if index >= 0 else Path(name).name


def _tracking_connect(*args, **kwargs):
    conn = _connect(*args, **kwargs)
    stack = [f for f in traceback.extract_stack()[:-1] if "/openminion/" in f.filename.replace("\\", "/")]
    OPENED[id(conn)] = [f"{_relative(f.filename)}:{f.lineno} {f.name}" for f in reversed(stack[-4:])]
    return conn


sqlite3.connect = sqlite3.dbapi2.connect = _tracking_connect

from typer.testing import CliRunner  # noqa: E402

from openminion.modules.memory.cli import _build_app  # noqa: E402
from openminion.modules.memory.models import MemoryCandidate  # noqa: E402
from openminion.modules.memory.storage.audit import default_memory_audit_db_path  # noqa: E402
from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore  # noqa: E402


def same_file(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def connected_to(obj, path):
    if type(obj) is not sqlite3.Connection:
        return False
    try:
        files = [row[2] for row in obj.execute("PRAGMA database_list") if row[2]]
    except sqlite3.ProgrammingError:
        return False
    return any(same_file(name, path) for name in files)


def open_connections(path):
    return [
        OPENED.get(id(obj), ["(not opened through sqlite3.connect)"])
        for obj in gc.get_objects()
        if connected_to(obj, path)
    ]


def try_rename(path):
    try:
        os.replace(path, f"{path}.renamed")
        os.replace(f"{path}.renamed", path)
        return "ok"
    except PermissionError as exc:
        return f"PermissionError [WinError {getattr(exc, 'winerror', None)}]"


def try_delete(path):
    try:
        os.remove(path)
        return "ok"
    except PermissionError as exc:
        return f"PermissionError [WinError {getattr(exc, 'winerror', None)}]"


def seed(db_path, candidate_ids):
    store = SQLiteMemoryStore(db_path, artifactctl=None)
    for candidate_id in candidate_ids:
        store.candidate_put(
            MemoryCandidate(
                candidate_id=candidate_id,
                session_id="s1",
                proposed_scope="session:s1",
                type="fact",
                content="proposed fact",
            )
        )
    store.close()


def in_process(root):
    db_path = root / "test.db"
    audit_path = default_memory_audit_db_path(db_path)
    seed(db_path, ["c1"])
    gc.collect()
    result = CliRunner().invoke(
        _build_app(), ["approve", "c1", "--reviewer", "agent", "--db", str(db_path)]
    )
    row = {
        "command": "memctl approve c1 --reviewer agent --db <tmp>/test.db (CliRunner, GC disabled)",
        "exit code": result.exit_code,
        "output": result.output.strip(),
        "audit database": audit_path.name,
    }
    sites = open_connections(audit_path)
    row["after the command returns"] = {
        "open connections to the audit database": len(sites),
        "opened at": sites,
        "rename": try_rename(audit_path),
    }
    gc.set_debug(gc.DEBUG_SAVEALL)
    found = gc.collect()
    gc.set_debug(0)
    unreachable = Counter(
        f"{type(obj).__module__}.{type(obj).__qualname__}"
        for obj in gc.garbage
        if type(obj).__module__.startswith("openminion")
    )
    audit_connection_unreachable = any(connected_to(obj, audit_path) for obj in gc.garbage)
    gc.garbage.clear()
    gc.collect()
    row["cyclic collector"] = {
        "unreachable objects found": found > 0,
        "openminion objects among them": dict(sorted(unreachable.items())),
        "audit database connection among them": audit_connection_unreachable,
    }
    row["after the cyclic collector"] = {
        "open connections to the audit database": len(open_connections(audit_path)),
        "rename": try_rename(audit_path),
        "delete": try_delete(audit_path),
    }
    return row


def separate_process(root):
    db_path = root / "test.db"
    audit_path = default_memory_audit_db_path(db_path)
    seed(db_path, ["c2"])
    completed = subprocess.run(
        [sys.executable, "-m", "openminion.modules.memory", "approve", "c2",
         "--reviewer", "agent", "--db", str(db_path)],
        env={**os.environ, "OPENMINION_HOME": str(root / "home"),
             "OPENMINION_DATA_ROOT": str(root / "data")},
        capture_output=True,
        text=True,
        timeout=600,
    )
    return {
        "command": "python -m openminion.modules.memory approve c2 --reviewer agent --db <tmp>/test.db",
        "exit code": completed.returncode,
        "output": completed.stdout.strip(),
        "files next to test.db after exit": sorted(
            p.name for p in root.iterdir() if p.name.startswith("test.audit")
        ),
        "rename right after exit": try_rename(audit_path),
        "delete right after exit": try_delete(audit_path),
    }


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        (Path(tmp) / "a").mkdir()
        (Path(tmp) / "b").mkdir()
        report = {
            "in-process (CliRunner)": in_process(Path(tmp) / "a"),
            "separate memctl process": separate_process(Path(tmp) / "b"),
        }
    print(json.dumps(report, indent=2))
    print(sys.version.split()[0], sys.platform)


if __name__ == "__main__":
    main()
