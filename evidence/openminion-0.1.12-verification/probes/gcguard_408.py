"""pytest plugin for #408: keep the cyclic garbage collector from hiding open handles.

    python -m pytest -p gcguard_408 ...   (this directory on PYTHONPATH;
                                           GCGUARD_OUT=<report.json>)

- Disables the cyclic collector when the plugin is imported (before the tests
  and openminion are imported) and checks that it is still disabled when each
  test starts and ends.
- Replaces gc.collect with a wrapper that counts calls. While a test runs
  (setup, call and teardown), a call is recorded with its caller and does NOT
  collect, so nothing can close a leaked connection behind the test's back.
  Outside tests (pytest's own end-of-session cleanup) it collects as usual.
- Records every sqlite3.connect call site (by connection id) and, after each
  test's teardown, lists the file-backed sqlite3 connections that are still
  open (found through gc.get_objects(), which does not collect).
- For tests that use tmp_path, renames every file under it to a new name and
  back after teardown; on Windows that fails while any handle is open.

Writes one JSON report (per test: outcome of each phase, gc state, gc.collect
calls, open connections, rename results). It does not change any test.
"""

from __future__ import annotations

import gc
import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path

gc.disable()
_REAL_COLLECT = gc.collect
_STATE = {"in_test": False, "current": None}
_OPENED: dict[int, str] = {}
_TMP_PATHS: dict = {}
_REPORT: dict = {
    "python": sys.version.split()[0],
    "sqlite": sqlite3.sqlite_version,
    "platform": sys.platform,
    "gc_disabled_at_plugin_import": not gc.isenabled(),
    "tests": {},
    "gc_collect_calls_outside_tests": 0,
}


def _site() -> str:
    frames = traceback.extract_stack()[:-2]
    keep = [f for f in frames if "/tests/" in f.filename.replace("\\", "/") or "/openminion/" in f.filename.replace("\\", "/")]
    chosen = (keep or frames)[-3:]
    return " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(chosen))


def _counting_collect(*args, **kwargs):
    if _STATE["in_test"]:
        entry = _REPORT["tests"][_STATE["current"]]
        entry["gc_collect_calls"].append(_site())
        return 0
    _REPORT["gc_collect_calls_outside_tests"] += 1
    return _REAL_COLLECT(*args, **kwargs)


gc.collect = _counting_collect
_REAL_CONNECT = sqlite3.connect


def _tracking_connect(*args, **kwargs):
    conn = _REAL_CONNECT(*args, **kwargs)
    _OPENED[id(conn)] = _site()
    return conn


sqlite3.connect = _tracking_connect
sqlite3.dbapi2.connect = _tracking_connect


def _open_file_connections() -> list[dict]:
    found = []
    for obj in gc.get_objects():
        if type(obj) is not sqlite3.Connection:
            continue
        try:
            names = [row[2] for row in obj.execute("PRAGMA database_list") if row[2]]
        except sqlite3.ProgrammingError as exc:  # closed, or another thread's
            if "closed" in str(exc).lower():
                continue
            names = ["(other thread)"]
        if names:
            found.append({"files": [Path(n).name for n in names], "opened_at": _OPENED.get(id(obj), "(unknown)")})
    return found


def _rename_round_trip(root: Path) -> list[dict]:
    results = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        moved = path.with_name(path.name + ".gcguard")
        try:
            os.replace(path, moved)
            os.replace(moved, path)
            results.append({"file": str(path.relative_to(root)), "rename": "ok"})
        except OSError as exc:
            results.append({"file": str(path.relative_to(root)), "rename": f"{type(exc).__name__} [WinError {getattr(exc, 'winerror', None)}]"})
    return results


def _begin(item):
    _STATE["current"] = item.nodeid
    _STATE["in_test"] = True
    _REPORT["tests"][item.nodeid] = {
        "gc_enabled_at_setup": gc.isenabled(),
        "gc_collect_calls": [],
        "phases": {},
    }


def pytest_runtest_logreport(report):
    entry = _REPORT["tests"].get(report.nodeid)
    if entry is not None:
        entry["phases"][report.when] = report.outcome
        if report.failed:
            entry.setdefault("failure", {})[report.when] = str(report.longrepr).splitlines()[-1][:400]


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    yield
    # funcargs is cleared at teardown, so remember tmp_path once fixtures are set up.
    _TMP_PATHS[item.nodeid] = (getattr(item, "funcargs", None) or {}).get("tmp_path")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _begin(item)
    yield
    entry = _REPORT["tests"].get(item.nodeid)
    _STATE["in_test"] = False
    if entry is None:
        return
    entry["gc_enabled_after_teardown"] = gc.isenabled()
    entry["open_file_connections_after_teardown"] = _open_file_connections()
    tmp_path = _TMP_PATHS.pop(item.nodeid, None)
    if tmp_path is not None:
        entry["tmp_path_rename_after_teardown"] = _rename_round_trip(Path(tmp_path))


def pytest_sessionfinish(session, exitstatus):
    tests = _REPORT["tests"]
    _REPORT["summary"] = {
        "tests": len(tests),
        "passed": sum(1 for t in tests.values() if all(v == "passed" for v in t["phases"].values())),
        "gc_enabled_during_any_test": any(t["gc_enabled_at_setup"] or t.get("gc_enabled_after_teardown") for t in tests.values()),
        "gc_collect_calls_during_tests": sum(len(t["gc_collect_calls"]) for t in tests.values()),
        "tests_with_open_file_connections_after_teardown": sum(1 for t in tests.values() if t.get("open_file_connections_after_teardown")),
        "tmp_path_renames_failed": sum(
            1 for t in tests.values() for r in t.get("tmp_path_rename_after_teardown", []) if r["rename"] != "ok"
        ),
        "exitstatus": int(exitstatus),
    }
    out = os.environ.get("GCGUARD_OUT")
    if out:
        Path(out).write_text(json.dumps(_REPORT, indent=2) + "\n", encoding="utf-8")
    print("\ngcguard_408:", json.dumps(_REPORT["summary"]))
