"""pytest plugin: who holds the SQLite file when a delete or replace fails?

Load it with `-p trackconn` (its folder on PYTHONPATH). It wraps sqlite3.connect
to remember where each connection was opened (frames under src/openminion/ and
tests/), and wraps os.unlink and pathlib.Path.replace: when one of them raises
PermissionError, it prints, on the real stderr, the sqlite3 connections still
open on that file and where they were opened, then re-raises.
"""

import gc
import os
import pathlib
import sqlite3
import sys
import traceback

OPENED = {}
_connect = sqlite3.connect
_unlink = os.unlink
_replace = pathlib.Path.replace
_last = [None]


def _site(frame):
    name = frame.filename.replace("\\", "/")
    for marker in ("/src/openminion/", "/tests/"):
        index = name.find(marker)
        if index >= 0:
            return f"{name[index + 1:]}:{frame.lineno} {frame.name}"
    return None


def _tracking_connect(*args, **kwargs):
    conn = _connect(*args, **kwargs)
    sites = [site for site in (_site(f) for f in traceback.extract_stack()[:-1]) if site]
    OPENED[id(conn)] = list(reversed(sites[-4:]))
    return conn


def _same(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _report(action, path):
    holders = []
    for obj in gc.get_objects():
        if type(obj) is not sqlite3.Connection:
            continue
        try:
            files = [row[2] for row in obj.execute("PRAGMA database_list") if row[2]]
        except sqlite3.ProgrammingError:
            continue
        if any(_same(name, path) for name in files):
            holders.append(OPENED.get(id(obj), ["(opened before the plugin loaded)"]))
    message = f"[trackconn] {action} {pathlib.Path(path).name} failed; open connections: {len(holders)}"
    if (message, str(holders)) == _last[0]:
        return
    _last[0] = (message, str(holders))
    print(message, file=sys.__stderr__)
    for holder in holders:
        print("    " + " <- ".join(holder), file=sys.__stderr__)


def _tracking_unlink(path, *args, **kwargs):
    try:
        return _unlink(path, *args, **kwargs)
    except PermissionError:
        _report("delete", path)
        raise


def _tracking_replace(self, target):
    try:
        return _replace(self, target)
    except PermissionError:
        _report("replace onto", target)
        raise


sqlite3.connect = sqlite3.dbapi2.connect = _tracking_connect
os.unlink = _tracking_unlink
pathlib.Path.replace = _tracking_replace
