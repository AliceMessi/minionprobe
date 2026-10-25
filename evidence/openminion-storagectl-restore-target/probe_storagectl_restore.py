"""`storagectl restore` with the real CLI: what happens to the restore target?

No test code is involved. Each storagectl step runs as its own process
(`python -m openminion.modules.storage`, the `storagectl` entry point), with
OPENMINION_HOME and OPENMINION_DATA_ROOT under a temporary directory.

Setup: `storagectl migrate --namespace session --sqlite <tmp>/session.db`; the
probe adds a one-row table `probe_marker` = `before-backup` (its own
connection, closed right away); `storagectl backup` takes a snapshot; the probe
sets the marker to `after-backup`.

Cases, each followed by the exit code, the last stderr line and the target's
state compared with the snapshot (marker, tables, bytes, leftover temp file):
A. `storagectl restore --sqlite <tmp>/session.db --snapshot-path <snapshot>`
   (restore over the live database);
B. the same onto a new path, `<tmp>/restored-b.db`;
C. control: `MigrationRunner.restore(...)` onto the live database, in its own
   process. That is the call storagectl's restore branch makes, without the
   StorageEngine that storagectl opens on the same path first;
D. control: the same onto a new path, `<tmp>/restored-d.db`;
E. trace: case A and case B again, in one process that calls `cli.main([...])`
   with sqlite3.connect wrapped to remember where each connection was opened.
   At the replace onto the target it prints the connections still open on that
   path and the size of the `-wal` file next to it; then whether the target
   still equals the snapshot after the replace, after `MigrationRunner.detect`
   (which storagectl runs next) and after `cli.main` returns.

Run from an openminion source checkout: PYTHONPATH="src;." python probe_storagectl_restore.py
(PYTHONPATH=src:. on Linux)
"""

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

TRACE_SCRIPT = r'''
import gc, hashlib, os, pathlib, sqlite3, sys, traceback
target, snapshot = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
OPENED = {}
_connect = sqlite3.connect
_replace = pathlib.Path.replace

def site(frame):
    name = frame.filename.replace("\\", "/")
    index = name.find("/openminion/")
    return f"{name[index + 1:]}:{frame.lineno} {frame.name}" if index >= 0 else None

def tracking_connect(*args, **kwargs):
    conn = _connect(*args, **kwargs)
    sites = [s for s in (site(f) for f in traceback.extract_stack()[:-1]) if s]
    OPENED[id(conn)] = list(reversed(sites[-8:]))
    return conn

def same(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))

def equals_snapshot(when):
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
    print(f"TARGET {when}: equals the snapshot: {digest(target) == digest(snapshot)}", flush=True)

def tracking_replace(self, dest):
    if not same(dest, target):
        return _replace(self, dest)
    holders = []
    for obj in gc.get_objects():
        if type(obj) is not sqlite3.Connection:
            continue
        try:
            files = [row[2] for row in obj.execute("PRAGMA database_list") if row[2]]
        except sqlite3.ProgrammingError:
            continue
        if any(same(name, dest) for name in files):
            holders.append(" <- ".join(OPENED.get(id(obj), ["(unknown)"])))
    wal = pathlib.Path(str(dest) + "-wal")
    print(f"REPLACE onto the target: {len(holders)} connection(s) open on it; "
          f"-wal: {wal.stat().st_size if wal.exists() else 'none'} bytes", flush=True)
    for holder in holders:
        print("  HOLDER " + holder, flush=True)
    try:
        result = _replace(self, dest)
    except PermissionError as exc:
        print(f"REPLACE raised {type(exc).__name__} winerror={getattr(exc, 'winerror', None)}", flush=True)
        raise
    equals_snapshot("right after the replace")
    return result

sqlite3.connect = sqlite3.dbapi2.connect = tracking_connect
pathlib.Path.replace = tracking_replace
from openminion.modules.storage import cli
from openminion.modules.storage.migrations import MigrationRunner
_detect = MigrationRunner.detect
def tracking_detect(self, *args, **kwargs):
    state = _detect(self, *args, **kwargs)
    equals_snapshot("after MigrationRunner.detect")
    return state
MigrationRunner.detect = tracking_detect
try:
    code = cli.main(["restore", "--namespace", "session", "--sqlite", str(target),
                     "--snapshot-path", str(snapshot)])
except Exception as exc:
    print("RAISED " + type(exc).__name__, flush=True)
    code = 1
equals_snapshot("after cli.main returned")
print("EXIT", code, flush=True)
'''

CONTROL_SCRIPT = r'''
import sys
from openminion.modules.storage.migrations import MigrationRunner
from openminion.modules.storage.migrations.module_ids import get_module_application_id
db, snapshot = sys.argv[1], sys.argv[2]
runner = MigrationRunner(
    module_id="session", db_path=db,
    module_application_id=get_module_application_id("session"),
)
runner.restore(snapshot_path=snapshot, target_db_path=db)
'''


def run(root, args, *, script=None):
    env = {
        **os.environ,
        "OPENMINION_HOME": str(root / "home"),
        "OPENMINION_DATA_ROOT": str(root / "data"),
    }
    cmd = [sys.executable, "-c", script, *args] if script else [
        sys.executable, "-m", "openminion.modules.storage", *args
    ]
    done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)

    def clean(text):
        return text.replace(str(root), "<tmp>").replace(str(root).replace("\\", "\\\\"), "<tmp>")

    return done.returncode, clean(done.stdout), clean(done.stderr)


def tables(db_path):
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def marker(db_path):
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        try:
            return conn.execute("SELECT v FROM probe_marker").fetchone()[0]
        except sqlite3.Error as exc:
            return f"(none: {exc})"


def set_marker(db_path, value, *, create=False):
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn, conn:
        if create:
            conn.execute("CREATE TABLE probe_marker(v TEXT NOT NULL)")
            conn.execute("INSERT INTO probe_marker(v) VALUES (?)", (value,))
        else:
            conn.execute("UPDATE probe_marker SET v = ?", (value,))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def target_state(root, db_path, snapshot):
    state = {"file exists": db_path.exists()}
    if db_path.exists():
        snap_tables, got = tables(snapshot), tables(db_path)
        state.update({
            "marker": marker(db_path),
            "bytes equal the snapshot": digest(db_path) == digest(snapshot),
            "snapshot tables missing": len(snap_tables - got),
            "tables not in the snapshot": len(got - snap_tables),
        })
    state["leftover *.restore.tmp"] = sorted(p.name for p in root.glob("*.restore.tmp"))
    for leftover in root.glob("*.restore.tmp"):
        leftover.unlink()
    return state


def last_line(text):
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    line = lines[-1]
    # Windows error text is localized; keep the error class and code only.
    for code in ("[WinError 5]", "[WinError 32]"):
        if code in line:
            return line.split(code)[0] + code + " (localized message trimmed)"
    return line


def main():
    report = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        live = root / "session.db"
        code, out, err = run(root, ["migrate", "--namespace", "session", "--sqlite", str(live)])
        assert code == 0, err
        set_marker(live, "before-backup", create=True)
        code, out, err = run(root, ["backup", "--namespace", "session", "--sqlite", str(live),
                                    "--snapshot-root", str(root / "snapshots")])
        assert code == 0, err
        snapshot = Path(json.loads(out)["backup"]["snapshot_path"].replace("<tmp>", str(root)))
        report["setup"] = {
            "migrate and backup exit codes": [0, 0],
            "snapshot marker": marker(snapshot),
            "snapshot tables": len(tables(snapshot)),
        }

        for label, target in (
            ("A. storagectl restore onto the live database", live),
            ("B. storagectl restore onto a new path", root / "restored-b.db"),
        ):
            if target == live:
                set_marker(live, "after-backup")
            code, out, err = run(root, ["restore", "--namespace", "session", "--sqlite",
                                        str(target), "--snapshot-path", str(snapshot)])
            ok = json.loads(out).get("ok") if code == 0 and out.strip() else None
            report[label] = {"exit code": code, "printed ok": ok, "last stderr line": last_line(err),
                             "target afterwards": target_state(root, target, snapshot)}

        for label, target in (
            ("C. control: MigrationRunner.restore onto the live database", live),
            ("D. control: MigrationRunner.restore onto a new path", root / "restored-d.db"),
        ):
            if target == live:
                set_marker(live, "after-backup")
            code, out, err = run(root, [str(target), str(snapshot)], script=CONTROL_SCRIPT)
            report[label] = {"exit code": code, "last stderr line": last_line(err),
                             "target afterwards": target_state(root, target, snapshot)}

        for label, target in (
            ("E. trace: storagectl restore onto the live database, in-process", live),
            ("E. trace: storagectl restore onto a new path, in-process", root / "restored-e.db"),
        ):
            if target == live:
                set_marker(live, "after-backup")
            code, out, err = run(root, [str(target), str(snapshot)], script=TRACE_SCRIPT)
            report[label] = {
                "output": [line for line in out.splitlines()
                           if line.lstrip().split(" ")[0] in {"REPLACE", "HOLDER", "TARGET", "RAISED", "EXIT"}],
                "target afterwards": target_state(root, target, snapshot),
            }
    print(json.dumps(report, indent=2))
    print(sys.version.split()[0], sys.platform, "SQLite", sqlite3.sqlite_version)


if __name__ == "__main__":
    main()
