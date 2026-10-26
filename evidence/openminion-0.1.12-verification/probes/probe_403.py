"""#403 on the installed package: memctl commands close what `_get_service` creates.

    python -I -X utf8 probe_403.py <work_dir> <out.json>

The cyclic garbage collector is disabled before openminion is imported, and
gc.collect is replaced by a counter that does not collect (the count must stay
0), so only explicit close() and reference counting can release a handle.

Every command runs in process through typer's CliRunner on the real memctl app
(`openminion.modules.memory.cli._build_app()`), each on a fresh database in its
own folder. Recording subclasses count close() on the MemoryService and
SQLiteMemoryAuditSink that memctl creates and on SQLiteMemoryStore (as the
upstream tests do). After each command the probe:
- reads the close counts (service, audit sink, store);
- lists sqlite3 connections still open on the database and audit database
  (gc.get_objects(), which does not collect);
- renames and then deletes every file in the folder, and removes the folder;
- calls service.close() again (the sink and store must stay closed once).

Cases:
1. Successful write commands: approve, reject, promote-approved,
   forget --memory, gc (each checks its stored effect and audit events).
2. Failing write commands: approve of a missing candidate (exit 1),
   promote-approved where the store fails on the second candidate after an
   audited write (exit 1), and history made to raise an unexpected error.
3. Read commands still work and close: get, list, search, candidates,
   history, stats, inspect (with the #416 history summary through the audited
   store), and bare-store history through build_inspect_payload.
4. A service built by `_get_service(db)` outside a command stays open until
   its caller closes it; then the files can be removed.
5. A borrowed service (memctl's `_get_service` replaced by one returning a
   caller-owned service) stays open after the command.
6. Typed remote config (#394): `memctl list` with a `store.backend: remote`
   YAML against a local HTTP store closes its service.
"""

from __future__ import annotations

import gc

gc.disable()

import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
from collections import Counter  # noqa: E402
from contextlib import closing  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from pathlib import Path  # noqa: E402

GC_CALLS = {"n": 0}


def _counting_collect(*_args, **_kwargs):
    GC_CALLS["n"] += 1
    return 0


gc.collect = _counting_collect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

import yaml  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from openminion.modules.memory import cli as memory_cli  # noqa: E402
from openminion.modules.memory.diagnostics.cli import build_inspect_payload  # noqa: E402
from openminion.modules.memory.models import MemoryCandidate, MemoryRecord  # noqa: E402
from openminion.modules.memory.runtime.remote_transport import RemoteMemoryStore  # noqa: E402
from openminion.modules.memory.service import MemoryService  # noqa: E402
from openminion.modules.memory.storage import (  # noqa: E402
    AuditedMemoryStore,
    SQLiteMemoryAuditSink,
    default_memory_audit_db_path,
)
from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore  # noqa: E402

WORK = Path(sys.argv[1])
OUT = Path(sys.argv[2])


class Recorder:
    def __init__(self) -> None:
        self.services: list = []
        self.closes: Counter = Counter()
        rec = self

        class RecordingService(MemoryService):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                rec.services.append(self)

            def close(self):
                rec.closes[id(self)] += 1
                super().close()

        class RecordingSink(SQLiteMemoryAuditSink):
            def close(self):
                rec.closes[id(self)] += 1
                super().close()

        real_sqlite_close, real_remote_close = SQLiteMemoryStore.close, RemoteMemoryStore.close

        def sqlite_close(store):
            rec.closes[id(store)] += 1
            real_sqlite_close(store)

        def remote_close(store):
            rec.closes[id(store)] += 1
            real_remote_close(store)

        self._saved = (memory_cli.MemoryService, memory_cli.SQLiteMemoryAuditSink, real_sqlite_close, real_remote_close)
        memory_cli.MemoryService = RecordingService
        memory_cli.SQLiteMemoryAuditSink = RecordingSink
        SQLiteMemoryStore.close = sqlite_close
        RemoteMemoryStore.close = remote_close

    def restore(self) -> None:
        memory_cli.MemoryService, memory_cli.SQLiteMemoryAuditSink, SQLiteMemoryStore.close, RemoteMemoryStore.close = self._saved

    def counts(self, service) -> list[int]:
        audited = service._store  # noqa: SLF001
        return [self.closes[id(service)], self.closes[id(audited._sink)], self.closes[id(audited._store)]]  # noqa: SLF001


def same_file(a, b) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def open_connections(*paths: Path) -> list[str]:
    held = []
    for obj in gc.get_objects():
        if type(obj) is not sqlite3.Connection:
            continue
        try:
            files = [row[2] for row in obj.execute("PRAGMA database_list") if row[2]]
        except sqlite3.ProgrammingError:
            continue
        held.extend(Path(f).name for f in files if any(same_file(f, p) for p in paths))
    return held


def remove_files(folder: Path) -> dict:
    out = {}
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        moved = path.with_name(path.name + ".moved")
        try:
            os.replace(path, moved)
        except OSError as exc:
            out[path.name] = f"rename: {type(exc).__name__} [WinError {getattr(exc, 'winerror', None)}]"
            continue
        try:
            os.remove(moved)
            out[path.name] = "renamed and deleted"
        except OSError as exc:
            out[path.name] = f"delete: {type(exc).__name__} [WinError {getattr(exc, 'winerror', None)}]"
    try:
        shutil.rmtree(folder)
        out["(folder)"] = "removed"
    except OSError as exc:
        out["(folder)"] = f"{type(exc).__name__} [WinError {getattr(exc, 'winerror', None)}]"
    return out


def seed(db: Path, *candidates: tuple[str, str], keyed_history: bool = False) -> None:
    store = SQLiteMemoryStore(db)
    try:
        store.put(MemoryRecord(id="r1", scope="session:s1", type="fact", content="the sky is blue",
                               created_at="2024-01-01T00:00:00Z", updated_at="2024-01-01T00:00:00Z"))
        if keyed_history:
            store.put(MemoryRecord(id="v1", scope="agent:a", type="fact", key="pref:indent", title="Prefer tabs",
                                   content="I prefer tabs.", created_at="2026-10-01T00:00:00+00:00", updated_at="2026-10-01T00:00:00+00:00"))
            store.upsert("agent:a", "fact", "pref:indent", {"title": "Prefer spaces", "content": "I prefer spaces."})
        for cid, status in candidates:
            store.candidate_put(MemoryCandidate(candidate_id=cid, session_id="s1", proposed_scope="session:s1",
                                                type="fact", content=f"proposed fact {cid}", status=status))
    finally:
        store.close()


def audit_events(audit: Path) -> list[str]:
    if not audit.exists():
        return []
    with closing(sqlite3.connect(audit)) as conn:
        return [r[0] for r in conn.execute("SELECT event_type FROM memory_audit_events ORDER BY timestamp")]


def candidate_status(db: Path, cid: str) -> str:
    store = SQLiteMemoryStore(db)
    try:
        return str(store.candidate_get(cid).status)
    finally:
        store.close()


def run_command(folder: Path, args: list[str], *, setup=None, env=None, db: Path | None = None) -> dict:
    """Run one memctl command in process; return what it closed and what is left open."""
    recorder = Recorder()
    undo = setup() if setup else None
    try:
        res = CliRunner().invoke(memory_cli._build_app(), args, env=env)
    finally:
        if undo:
            undo()
        recorder.restore()
    row = {
        "args": [a if not os.path.isabs(a) else Path(a).name for a in args],
        "exit_code": res.exit_code,
        "output": res.output.strip()[:300],
        "exception": None if res.exception is None or isinstance(res.exception, SystemExit) else f"{type(res.exception).__name__}: {res.exception}",
        "services created": len(recorder.services),
        "_output_full": res.output,
    }
    if recorder.services:
        service = recorder.services[0]
        audited = service._store  # noqa: SLF001
        row["store"] = type(audited._store).__name__  # noqa: SLF001
        row["close calls after the command (service, sink, store)"] = recorder.counts(service)
        paths = [db, default_memory_audit_db_path(db)] if db else []
        row["open connections after the command"] = open_connections(*paths) if paths else []
        row["audit sink connection after the command"] = "closed" if getattr(audited._sink, "_conn", None) is None else "open"  # noqa: SLF001
        row["_service"] = service
        row["_recorder"] = recorder
    return row


def finish(row: dict, folder: Path, extra_close: bool = True) -> dict:
    service = row.pop("_service", None)
    recorder = row.pop("_recorder", None)
    row.pop("_output_full", None)
    row["files right after the command"] = remove_files(folder)
    if service is not None and extra_close:
        service.close()
        row["close calls after a second service.close() (service, sink, store)"] = recorder.counts(service)
    return row


def closed_once(row: dict) -> bool:
    files = row.get("files right after the command", {})
    return (row.get("close calls after the command (service, sink, store)") == [1, 1, 1]
            and row.get("open connections after the command") == []
            and row.get("audit sink connection after the command") == "closed"
            and bool(files) and all(v in ("renamed and deleted", "removed") for v in files.values())
            and row.get("close calls after a second service.close() (service, sink, store)") == [2, 1, 1])


class RemoteHandler(BaseHTTPRequestHandler):
    requests: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        RemoteHandler.requests.append(body.get("operation"))
        data = json.dumps({"ok": True, "data": {"records": [{"id": "remote-1", "scope": "agent:alpha", "type": "fact",
                                                             "content": "Prefers vim."}]}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def main() -> int:
    quiet()
    clear_override_env()
    info = banner("probe_403", gc_enabled=gc.isenabled())
    checks = Checks()
    results: dict = {}
    root = WORK / "run"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    def folder_db(name: str) -> tuple[Path, Path, Path]:
        folder = root / name
        folder.mkdir()
        db = folder / "test.db"
        return folder, db, default_memory_audit_db_path(db)

    # 1. Successful write commands.
    writes = {
        "approve": (["approve", "c1", "--reviewer", "agent"], [("c1", "proposed")], "Approved: c1", "approved", ["memory.candidate.update"]),
        "reject": (["reject", "c1", "--reviewer", "agent", "--note", "not relevant"], [("c1", "proposed")], "Rejected: c1", "rejected", ["memory.candidate.update"]),
        "promote-approved": (["promote-approved", "--session-id", "s1", "--target-scope", "global:all"], [("c1", "approved")], "Promoted: 1, Failed: 0", "promoted", ["memory.candidate.promote"]),
    }
    for name, (args, cands, output, status, events) in writes.items():
        folder, db, audit = folder_db(f"write-{name}")
        seed(db, *cands)
        row = run_command(folder, [*args, "--db", str(db)], db=db)
        row["stored candidate status"] = candidate_status(db, "c1")
        row["audit events"] = audit_events(audit)
        finish(row, folder)
        results[f"memctl {name}"] = row
        checks.check(f"memctl {name} (success): effect stored and audited", row["exit_code"] == 0 and output in row["output"]
                     and row["stored candidate status"] == status and row["audit events"] == events,
                     {k: row[k] for k in ("exit_code", "output", "stored candidate status", "audit events")})
        checks.check(f"memctl {name} (success): service/sink/store closed once at command exit, no open connection, files renamed and deleted",
                     closed_once(row), {k: v for k, v in row.items() if k.startswith(("close", "open", "audit sink", "files"))})

    folder, db, audit = folder_db("write-forget")
    seed(db)
    row = run_command(folder, ["forget", "--memory", "r1", "--reason", "probe", "--json", "--db", str(db)], db=db)
    row["audit events"] = audit_events(audit)
    finish(row, folder)
    results["memctl forget --memory r1"] = row
    checks.check("memctl forget --memory (success): deleted and audited", row["exit_code"] == 0 and '"deleted": true' in row["output"]
                 and any(e in ("memory.record.delete", "memory.record.tombstone") for e in row["audit events"]),
                 {k: row[k] for k in ("exit_code", "output", "audit events")})
    checks.check("memctl forget --memory (success): closed once, no open connection, files renamed and deleted", closed_once(row),
                 {k: v for k, v in row.items() if k.startswith(("close", "open", "audit sink", "files"))})

    folder, db, audit = folder_db("write-gc")
    seed(db)
    row = run_command(folder, ["gc", "--json", "--db", str(db)], db=db)
    finish(row, folder)
    results["memctl gc"] = row
    checks.check("memctl gc (success): exit 0 and a GC result printed", row["exit_code"] == 0 and "GC: deleted_records=" in row["output"],
                 {k: row[k] for k in ("exit_code", "output", "exception")})
    checks.check("memctl gc (success): closed once, no open connection, files renamed and deleted", closed_once(row),
                 {k: v for k, v in row.items() if k.startswith(("close", "open", "audit sink", "files"))})

    # 2. Failing write commands.
    folder, db, audit = folder_db("fail-approve-missing")
    seed(db)
    row = finish(run_command(folder, ["approve", "missing", "--reviewer", "agent", "--db", str(db)], db=db), folder)
    results["memctl approve missing (fails)"] = row
    checks.check("memctl approve of a missing candidate fails (exit 1, Error) and closes once; files removed",
                 row["exit_code"] == 1 and "Error:" in row["output"] and closed_once(row), row)

    folder, db, audit = folder_db("fail-promote-after-write")
    seed(db, ("c1", "approved"), ("c2", "approved"))
    real_promote = SQLiteMemoryStore.promote_candidate

    def break_promote():
        def promote(store, candidate_id, target_scope):
            if candidate_id == "c2":
                raise RuntimeError("store failure")
            return real_promote(store, candidate_id, target_scope)
        SQLiteMemoryStore.promote_candidate = promote
        return lambda: setattr(SQLiteMemoryStore, "promote_candidate", real_promote)

    row = run_command(folder, ["promote-approved", "--session-id", "s1", "--target-scope", "global:all", "--db", str(db)], setup=break_promote, db=db)
    row["audit events"] = audit_events(audit)
    finish(row, folder)
    results["memctl promote-approved, store fails on the second candidate (fails)"] = row
    checks.check("memctl promote-approved failing after an audited write (exit 1) closes once; files removed",
                 row["exit_code"] == 1 and "Promoted: 1, Failed: 1" in row["output"] and row["audit events"] == ["memory.candidate.promote"]
                 and closed_once(row), row)

    folder, db, audit = folder_db("fail-history-raises")
    seed(db)
    real_history = SQLiteMemoryStore.history

    def break_history():
        def history(*_a, **_k):
            raise RuntimeError("history failed")
        SQLiteMemoryStore.history = history
        return lambda: setattr(SQLiteMemoryStore, "history", real_history)

    row = finish(run_command(folder, ["history", "--scope", "session:s1", "--type", "fact", "--key", "k", "--db", str(db)],
                             setup=break_history, db=db), folder)
    results["memctl history raising an unexpected error (fails)"] = row
    checks.check("memctl command raising an unexpected error (RuntimeError escapes) closes once; files removed",
                 row["exit_code"] == 1 and (row["exception"] or "").startswith("RuntimeError") and closed_once(row), row)

    # 3. Read commands.
    reads = {
        "get": (["get", "r1", "--json"], '"the sky is blue"'),
        "list": (["list", "--scope", "session:s1", "--json"], '"r1"'),
        "search": (["search", "sky", "--scope", "session:s1", "--json"], '"r1"'),
        "candidates": (["candidates", "--session-id", "s1", "--json"], '"c1"'),
        "history": (["history", "--scope", "agent:a", "--type", "fact", "--key", "pref:indent", "--json"], '"Prefer spaces"'),
        "stats": (["stats", "--json"], '"active_record_count"'),
        "inspect": (["inspect", "--scope", "agent:a", "--json"], '"history_summary"'),
    }
    for name, (args, needle) in reads.items():
        folder, db, audit = folder_db(f"read-{name}")
        seed(db, ("c1", "proposed"), keyed_history=True)
        row = run_command(folder, [*args, "--db", str(db)], db=db)
        full = row["_output_full"]
        if name == "inspect":
            try:
                summary = json.loads(full)["history_summary"]
            except Exception as exc:  # noqa: BLE001
                summary = f"{type(exc).__name__}: {exc}"
            row["history_summary"] = {k: summary.get(k) for k in ("scope", "type", "key", "depth")} if isinstance(summary, dict) else summary
        row["expected text in output"] = needle in full
        row["audit database created"] = audit.exists()
        finish(row, folder)
        results[f"memctl {name}"] = row
        ok = row["exit_code"] == 0 and row["expected text in output"] and closed_once(row) and not row["audit database created"]
        if name == "inspect":
            ok = ok and row["history_summary"] == {"scope": "agent:a", "type": "fact", "key": "pref:indent", "depth": 2}
        checks.check(f"memctl {name} (read) works, opens no audit database, closes once; files removed", ok, row)

    folder, db, _audit = folder_db("bare-history")
    seed(db, keyed_history=True)
    bare = MemoryService(SQLiteMemoryStore(db))
    trace = folder / "trace.jsonl"
    trace.write_text("", encoding="utf-8")
    payload = build_inspect_payload(service=bare, scope="agent:a", db_path=db, trace_file=trace)
    got = {k: (payload["history_summary"] or {}).get(k) for k in ("scope", "type", "key", "depth")}
    bare.close()
    results["build_inspect_payload over the bare SQLiteMemoryStore"] = got
    checks.check("bare-store history summary (build_inspect_payload, MemoryService over SQLiteMemoryStore): depth 2",
                 got == {"scope": "agent:a", "type": "fact", "key": "pref:indent", "depth": 2}, got)
    results["bare-history files"] = remove_files(folder)

    # 4. Direct factory caller owns the close.
    folder, db, audit = folder_db("direct-factory")
    seed(db, ("c1", "proposed"))
    recorder = Recorder()  # restored after the caller's second close below
    service = memory_cli._get_service(str(db))
    service.candidate_update("c1", {"status": "approved"})
    direct = {"open audit connections before close": open_connections(audit),
              "close calls before close (service, sink, store)": recorder.counts(service)}
    try:
        os.replace(audit, audit.with_name("moved.db"))
        os.replace(audit.with_name("moved.db"), audit)
        direct["rename of the audit database before close"] = "ok"
    except OSError as exc:
        direct["rename of the audit database before close"] = f"{type(exc).__name__} [WinError {getattr(exc, 'winerror', None)}]"
    service.close()
    direct["close calls after close (service, sink, store)"] = recorder.counts(service)
    direct["open connections after close"] = open_connections(db, audit)
    direct["stored candidate status"] = candidate_status(db, "c1")
    direct["files after close"] = remove_files(folder)
    service.close()
    direct["close calls after a second close (service, sink, store)"] = recorder.counts(service)
    recorder.restore()
    results["_get_service(db) outside a command, closed by its caller"] = direct
    checks.check("a service from _get_service(db) outside a command stays open until its caller closes it",
                 direct["open audit connections before close"] == ["test.audit.db"] and direct["close calls before close (service, sink, store)"] == [0, 0, 0],
                 {k: direct[k] for k in ("open audit connections before close", "close calls before close (service, sink, store)", "rename of the audit database before close")})
    checks.check("the caller's close() closes service, sink and store once; files then renamed and deleted; second close is a no-op",
                 direct["close calls after close (service, sink, store)"] == [1, 1, 1] and direct["open connections after close"] == []
                 and direct["stored candidate status"] == "approved"
                 and all(v in ("renamed and deleted", "removed") for v in direct["files after close"].values())
                 and direct["close calls after a second close (service, sink, store)"] == [2, 1, 1], direct)

    # 5. Borrowed service.
    folder, db, audit = folder_db("borrowed")
    seed(db, ("c1", "proposed"))
    sink = SQLiteMemoryAuditSink(audit)
    borrowed = MemoryService(AuditedMemoryStore(SQLiteMemoryStore(db), sink=sink))
    saved = memory_cli._get_service
    memory_cli._get_service = lambda *_a, **_k: borrowed
    try:
        res = CliRunner().invoke(memory_cli._build_app(), ["approve", "c1", "--reviewer", "agent", "--db", str(db)])
    finally:
        memory_cli._get_service = saved
    borrow = {"exit_code": res.exit_code, "output": res.output.strip(),
              "open audit connections after the command": open_connections(audit),
              "borrowed service still usable (candidate status)": str(borrowed.candidate_get("c1").status)}
    sink.close()
    borrow["open audit connections after the owner closes the sink"] = open_connections(audit)
    borrow["files after the owner closes"] = remove_files(folder)
    results["borrowed service (memctl's _get_service returns a caller-owned service)"] = borrow
    checks.check("a borrowed service stays open after the command and is closed by its owner",
                 borrow["exit_code"] == 0 and borrow["open audit connections after the command"] == ["test.audit.db"]
                 and borrow["borrowed service still usable (candidate status)"] == "approved"
                 and borrow["open audit connections after the owner closes the sink"] == []
                 and all(v in ("renamed and deleted", "removed") for v in borrow["files after the owner closes"].values()), borrow)

    # 6. Typed remote config.
    server = ThreadingHTTPServer(("127.0.0.1", 0), RemoteHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    folder = root / "remote"
    folder.mkdir()
    home = folder / "home"
    home.mkdir()
    cfg = folder / "memctl.yaml"
    cfg.write_text(yaml.safe_dump({"version": 1, "memctl": {
        "store": {"backend": "remote", "remote": {"endpoint": f"http://127.0.0.1:{server.server_address[1]}/memory", "timeout_seconds": 2.5, "max_retries": 0}},
        "defaults": {"confidence": {"user_said": 0.7}}, "promotion": {}, "retrieval": {}, "retention": {}}}), encoding="utf-8")
    row = run_command(folder, ["list", "--scope", "agent:alpha", "--json"],
                      env={"MEMCTL_CONFIG": str(cfg), "HOME": str(home), "USERPROFILE": str(home), "OPENMINION_HOME": None, "OPENMINION_DATA_ROOT": None})
    row["remote requests"] = list(RemoteHandler.requests)
    finish(row, folder)
    server.shutdown()
    results["memctl list with a store.backend: remote YAML (#394)"] = row
    checks.check("memctl list with the typed remote config (#394) works and closes its service once",
                 row["exit_code"] == 0 and '"remote-1"' in row["output"] and row["remote requests"] == ["list"]
                 and row.get("store") == "RemoteMemoryStore"
                 and row.get("close calls after the command (service, sink, store)") == [1, 1, 1]
                 and row.get("close calls after a second service.close() (service, sink, store)") == [2, 1, 1], row)

    checks.check("cyclic GC stayed disabled for the whole probe and gc.collect was never called",
                 not gc.isenabled() and GC_CALLS["n"] == 0, {"gc_enabled": gc.isenabled(), "gc.collect calls": GC_CALLS["n"]})
    summary = checks.summary()
    write_result(OUT, {"info": info, "results": results, "checks": checks.items, "summary": summary})
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
