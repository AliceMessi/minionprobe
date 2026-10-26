"""#402 on the installed package: `memctl inspect` reports the key history summary.

    python -I -X utf8 probe_402.py <work_dir> <out.json> [--postgres]

SQLite (always):
1. Seed a SQLite memory database with a keyed fact updated once (put, then
   upsert on the same scope/type/key: two versions), an unkeyed fact, a keyed
   fact with one version, and a trace file with three events.
2. Real `memctl inspect --scope agent:a --db <db> --trace-file <trace>` through
   the installed `memctl` console script (separate process), with `--json` and
   without: `history_summary.depth == 2` for that key and the matching
   "Deepest history: agent:a fact pref:indent (depth 2)" line, through the
   audited store memctl builds; stats and trace output present.
3. `memctl history` for the key shows the same two versions.
4. Databases without keyed history (empty; unkeyed and single-version keys
   only): no summary in either mode.
5. build_inspect_payload with the service memctl builds (audited store), a
   MemoryService over the bare SQLiteMemoryStore, and bare/audited in-memory
   (mock) stores: SQLite reports depth 2 both ways; the mock store, which is
   not SQLite, reports none.
6. Command lifecycle (#418 is in the same release), in process through
   typer's CliRunner with the cyclic garbage collector disabled: a successful
   `memctl inspect` and one that fails (build_inspect_payload made to raise)
   close the service, its audit sink and its store once, leave no sqlite3
   connection open on the database or the audit database, and the files can
   be renamed and deleted right after the command.
PostgreSQL (with --postgres, OPENMINION_TEST_POSTGRES_URL set): a keyed fact
with two versions in a fresh schema; `memctl inspect --scope agent:a --json`
with a `store.backend: postgres` config, and build_inspect_payload over the
bare and audited PostgresMemoryStore: no history summary (the summary stays
SQLite-only), stats still reported.
"""

from __future__ import annotations

import gc

gc.disable()  # before openminion is imported; see check 6

import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from collections import Counter  # noqa: E402
from pathlib import Path  # noqa: E402

_REAL_COLLECT = gc.collect
GC_CALLS = {"n": 0}


def _counting_collect(*args, **kwargs):
    GC_CALLS["n"] += 1
    return 0


gc.collect = _counting_collect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize_text, write_result  # noqa: E402

from openminion.modules.memory import cli as memory_cli  # noqa: E402
from openminion.modules.memory.diagnostics.cli import build_inspect_payload  # noqa: E402
from openminion.modules.memory.models import MemoryRecord  # noqa: E402
from openminion.modules.memory.service import MemoryService  # noqa: E402
from openminion.modules.memory.storage import (  # noqa: E402
    AuditedMemoryStore,
    SQLiteMemoryAuditSink,
    default_memory_audit_db_path,
)
from openminion.modules.memory.storage.audit import InMemoryMemoryAuditSink  # noqa: E402
from openminion.modules.memory.storage.memory import InMemoryMemoryStore  # noqa: E402
from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore  # noqa: E402

WORK = Path(sys.argv[1])
OUT = Path(sys.argv[2])
POSTGRES = "--postgres" in sys.argv[3:]
DEEPEST = "Deepest history: agent:a fact pref:indent (depth 2)"
TRACE_EVENTS = [
    {"ts": "2026-10-01T00:00:01Z", "event": "memory.context.built", "agent_id": "agent:a"},
    {"ts": "2026-10-01T00:00:02Z", "event": "memory.reflection.completed", "agent_id": "agent:a"},
    {"ts": "2026-10-01T00:00:03Z", "event": "memory.retrieval.completed", "agent_id": "agent:a"},
]


def record(rid: str, key: str | None, title: str, content: str) -> MemoryRecord:
    return MemoryRecord(id=rid, scope="agent:a", type="fact", key=key, title=title, content=content,
                        created_at="2026-10-01T00:00:00+00:00", updated_at="2026-10-01T00:00:00+00:00")


def seed_history(store) -> None:
    store.put(record("v1", "pref:indent", "Prefer tabs", "I prefer tabs."))
    store.upsert("agent:a", "fact", "pref:indent", {"title": "Prefer spaces", "content": "I prefer spaces."})
    store.put(record("u1", None, "Unkeyed", "An unkeyed fact."))
    store.put(record("k1", "pref:theme", "Theme", "Dark theme."))


def seed_no_history(store) -> None:
    store.put(record("u1", None, "Unkeyed", "An unkeyed fact."))
    store.put(record("k1", "pref:theme", "Theme", "Dark theme."))


def memctl_exe() -> Path:
    return Path(sys.executable).with_name("memctl.exe" if os.name == "nt" else "memctl")


def cli_env(home: Path, extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENMINION_", "MEMCTL_"))}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONUTF8="1")
    env.update(extra or {})
    return env


def run_memctl(args: list[str], env: dict, cwd: Path) -> dict:
    proc = subprocess.run([str(memctl_exe()), *args], capture_output=True, text=True, env=env, cwd=str(cwd), timeout=600)
    return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr_tail": proc.stderr.strip().splitlines()[-3:]}


def summary_of(payload: dict | None):
    summary = (payload or {}).get("history_summary", "(missing)")
    if isinstance(summary, dict):
        return {k: summary.get(k) for k in ("scope", "type", "key", "depth")}
    return summary


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


def rename_and_delete(folder: Path) -> dict:
    out = {}
    for path in sorted(p for p in folder.iterdir() if p.is_file()):
        try:
            os.replace(path, path.with_name(path.name + ".moved"))
            os.remove(path.with_name(path.name + ".moved"))
            out[path.name] = "renamed and deleted"
        except OSError as exc:
            out[path.name] = f"{type(exc).__name__} [WinError {getattr(exc, 'winerror', None)}]"
    return out


class Recorder:
    """Counts close() on the services, audit sinks and SQLite stores memctl creates."""

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

        self._saved = (memory_cli.MemoryService, memory_cli.SQLiteMemoryAuditSink, SQLiteMemoryStore.close)
        real_store_close = SQLiteMemoryStore.close

        def store_close(store):
            rec.closes[id(store)] += 1
            real_store_close(store)

        memory_cli.MemoryService = RecordingService
        memory_cli.SQLiteMemoryAuditSink = RecordingSink
        SQLiteMemoryStore.close = store_close

    def restore(self) -> None:
        memory_cli.MemoryService, memory_cli.SQLiteMemoryAuditSink, SQLiteMemoryStore.close = self._saved

    def counts(self, service) -> list[int]:
        audited = service._store  # noqa: SLF001
        return [self.closes[id(service)], self.closes[id(audited._sink)], self.closes[id(audited._store)]]  # noqa: SLF001


def main() -> int:
    quiet()
    clear_override_env()
    info = banner("probe_402", gc_enabled=gc.isenabled(), postgres=POSTGRES)
    checks = Checks()
    results: dict = {}
    root = WORK / "run"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    home = root / "home"
    home.mkdir()
    env = cli_env(home)

    # 1. Seed.
    hist = root / "hist"
    hist.mkdir()
    db = hist / "memory.db"
    trace = hist / "trace.jsonl"
    trace.write_text("".join(json.dumps(e) + "\n" for e in TRACE_EVENTS), encoding="utf-8")
    store = SQLiteMemoryStore(db, artifactctl=None)
    seed_history(store)
    store.close()

    # 3. memctl history.
    res = run_memctl(["history", "--scope", "agent:a", "--type", "fact", "--key", "pref:indent", "--db", str(db), "--json"], env, root)
    titles = [r.get("title") for r in json.loads(res["stdout"])] if res["exit_code"] == 0 else res
    results["memctl history --scope agent:a --type fact --key pref:indent --json"] = titles
    checks.check("memctl history shows the key's two versions", isinstance(titles, list) and sorted(titles) == ["Prefer spaces", "Prefer tabs"], titles)

    # 2. memctl inspect, JSON and human.
    common = ["inspect", "--scope", "agent:a", "--db", str(db), "--trace-file", str(trace)]
    res = run_memctl([*common, "--json"], env, root)
    payload = json.loads(res["stdout"]) if res["exit_code"] == 0 else {}
    stats = payload.get("stats") or {}
    row = {"exit_code": res["exit_code"], "history_summary": payload.get("history_summary", "(missing)"),
           "stats.active_record_count": stats.get("active_record_count"),
           "stats.supersession_chain_count": stats.get("supersession_chain_count"),
           "stats keys": sorted(stats), "recent_trace_events": [e.get("event") for e in payload.get("recent_trace_events", [])],
           "last_gc": payload.get("last_gc"), "last_reflection": payload.get("last_reflection")}
    results["memctl inspect --scope agent:a --db <db> --trace-file <trace> --json"] = row
    checks.check("memctl inspect --json: history_summary for agent:a fact pref:indent with depth 2",
                 summary_of(payload) == {"scope": "agent:a", "type": "fact", "key": "pref:indent", "depth": 2}, summary_of(payload))
    checks.check("memctl inspect --json: stats still reported (3 active records)",
                 stats.get("active_record_count") == 3, stats.get("active_record_count"))
    checks.check("memctl inspect --json: trace output still reported (3 recent events, last GC-ish and reflection times)",
                 row["recent_trace_events"] == [e["event"] for e in TRACE_EVENTS]
                 and row["last_gc"] == "2026-10-01T00:00:01Z" and row["last_reflection"] == "2026-10-01T00:00:02Z",
                 {k: row[k] for k in ("recent_trace_events", "last_gc", "last_reflection")})
    res = run_memctl(common, env, root)
    lines = [line for line in res["stdout"].splitlines() if line.strip()]
    results["memctl inspect --scope agent:a --db <db> --trace-file <trace> (human)"] = {
        "exit_code": res["exit_code"], "output": sanitize_text(res["stdout"])}
    checks.check("memctl inspect (human): the Deepest history line for the key, depth 2", DEEPEST in res["stdout"],
                 [ln for ln in lines if "Deepest" in ln])
    checks.check("memctl inspect (human): stats and trace sections still printed",
                 "Recent trace events:" in res["stdout"] and "Last GC-ish event: 2026-10-01T00:00:01Z" in res["stdout"]
                 and "memory.retrieval.completed" in res["stdout"], [ln for ln in lines if "trace" in ln.lower() or "Last" in ln])

    # 4. No keyed history.
    for label, seeder in (("empty database", None), ("unkeyed and single-version keys only", seed_no_history)):
        folder = root / ("empty" if seeder is None else "nohist")
        folder.mkdir()
        path = folder / "memory.db"
        s = SQLiteMemoryStore(path, artifactctl=None)
        if seeder:
            seeder(s)
        s.close()
        rj = run_memctl(["inspect", "--scope", "agent:a", "--db", str(path), "--json"], env, root)
        rh = run_memctl(["inspect", "--scope", "agent:a", "--db", str(path)], env, root)
        pj = json.loads(rj["stdout"]) if rj["exit_code"] == 0 else {}
        results[f"memctl inspect, {label}"] = {"json exit": rj["exit_code"], "history_summary": pj.get("history_summary", "(missing)"),
                                               "human exit": rh["exit_code"], "Deepest history line": "Deepest history" in rh["stdout"]}
        checks.check(f"{label}: no history summary in JSON or human output",
                     rj["exit_code"] == 0 and rh["exit_code"] == 0 and "history_summary" in pj and pj["history_summary"] is None
                     and "Deepest history" not in rh["stdout"], results[f"memctl inspect, {label}"])

    # 5. build_inspect_payload with several services.
    svc = memory_cli._get_service(str(db))
    services = {
        "memctl's _get_service(db) (AuditedMemoryStore over SQLiteMemoryStore)": (svc, {"scope": "agent:a", "type": "fact", "key": "pref:indent", "depth": 2}),
        "MemoryService over the bare SQLiteMemoryStore": (MemoryService(SQLiteMemoryStore(db, artifactctl=None)), {"scope": "agent:a", "type": "fact", "key": "pref:indent", "depth": 2}),
    }
    mock = InMemoryMemoryStore()
    seed_history(mock)
    services["MemoryService over a bare in-memory (mock) store"] = (MemoryService(mock), None)
    services["MemoryService over AuditedMemoryStore(in-memory store)"] = (MemoryService(AuditedMemoryStore(mock, sink=InMemoryMemoryAuditSink())), None)
    for label, (service, want) in services.items():
        try:
            got = summary_of(build_inspect_payload(service=service, scope="agent:a", db_path=db, trace_file=trace))
        except Exception as exc:  # noqa: BLE001
            got = f"{type(exc).__name__}: {exc}"
        results[f"build_inspect_payload with {label}"] = got
        checks.check(f"build_inspect_payload, {label}: {'depth 2' if want else 'no summary'}", got == want, got)
    svc.close()

    # 6. Command lifecycle in process, GC disabled.
    from typer.testing import CliRunner

    lifecycle = {}
    for label, fail in (("successful inspect", False), ("failing inspect (build_inspect_payload raises)", True)):
        folder = root / ("life-ok" if not fail else "life-fail")
        folder.mkdir()
        path = folder / "memory.db"
        s = SQLiteMemoryStore(path, artifactctl=None)
        seed_history(s)
        s.close()
        recorder = Recorder()
        saved_payload = memory_cli.build_inspect_payload
        if fail:
            def boom(**_kwargs):
                raise RuntimeError("inspect failed")
            memory_cli.build_inspect_payload = boom
        try:
            res = CliRunner().invoke(memory_cli._build_app(), ["inspect", "--scope", "agent:a", "--db", str(path), "--json"])
        finally:
            memory_cli.build_inspect_payload = saved_payload
            recorder.restore()
        row = {"exit_code": res.exit_code,
               "exception": None if res.exception is None or isinstance(res.exception, SystemExit) else f"{type(res.exception).__name__}: {res.exception}",
               "services created": len(recorder.services)}
        if recorder.services:
            service = recorder.services[0]
            row["close calls after the command (service, sink, store)"] = recorder.counts(service)
            row["open connections to the database or audit database"] = open_connections(path, default_memory_audit_db_path(path))
            row["gc enabled"] = gc.isenabled()
            row["files right after the command"] = rename_and_delete(folder)
            service.close()
            row["close calls after a second service.close() (service, sink, store)"] = recorder.counts(service)
        lifecycle[label] = row
        ok_exit = (res.exit_code == 0 and '"history_summary"' in res.output) if not fail else (res.exit_code == 1 and "inspect failed" in (row["exception"] or ""))
        checks.check(f"in process, GC disabled, {label}: service, audit sink and store closed once; no open connection; files renamed and deleted",
                     ok_exit and row.get("close calls after the command (service, sink, store)") == [1, 1, 1]
                     and row.get("open connections to the database or audit database") == []
                     and all(v == "renamed and deleted" for v in row.get("files right after the command", {"none": "x"}).values())
                     and row.get("close calls after a second service.close() (service, sink, store)") == [2, 1, 1],
                     row)
    results["command lifecycle (in process, cyclic GC disabled)"] = lifecycle

    # PostgreSQL.
    if POSTGRES:
        results["postgres"] = postgres_case(checks, root, home)

    checks.check("cyclic GC stayed disabled and gc.collect was never called", not gc.isenabled() and GC_CALLS["n"] == 0,
                 {"gc_enabled": gc.isenabled(), "gc.collect calls": GC_CALLS["n"]})
    summary = checks.summary()
    write_result(OUT, {"info": info, "results": results, "checks": checks.items, "summary": summary})
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    return 0 if checks.all_pass else 1


def postgres_case(checks: Checks, root: Path, home: Path) -> dict:
    import sqlalchemy as sa
    import yaml

    from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore

    base = os.environ["OPENMINION_TEST_POSTGRES_URL"]
    admin = sa.create_engine(base, future=True)
    schema = f"probe402_{uuid.uuid4().hex[:10]}"
    with admin.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        server_version = conn.execute(sa.text("select version()")).scalar()
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}options=-csearch_path%3D{schema}"
    out: dict = {"server": server_version, "schema": "probe402_<random>"}
    engine = sa.create_engine(url, future=True)
    folder = root / "pg"
    folder.mkdir()
    trace = folder / "trace.jsonl"
    trace.write_text("", encoding="utf-8")
    try:
        store = PostgresMemoryStore(engine, database_path=folder / "m.db", artifactctl=None)
        seed_history(store)
        out["history depth for the key (store.history)"] = len(store.history("agent:a", "fact", "pref:indent"))
        with engine.connect() as conn:
            out["memory_records rows in the probe schema (server-side count)"] = conn.execute(
                sa.text("SELECT COUNT(*) FROM memory_records")).scalar()
        for label, svc_store in (("bare PostgresMemoryStore", store),
                                 ("AuditedMemoryStore(PostgresMemoryStore)", AuditedMemoryStore(store, sink=InMemoryMemoryAuditSink()))):
            payload = build_inspect_payload(service=MemoryService(svc_store), scope="agent:a", db_path=folder / "m.db", trace_file=trace)
            out[f"build_inspect_payload, {label}"] = {"history_summary": payload["history_summary"],
                                                      "stats.active_record_count": (payload["stats"] or {}).get("active_record_count")}
        cfg = folder / "memctl-postgres.yaml"
        cfg.write_text(yaml.safe_dump({"version": 1, "memctl": {
            "store": {"backend": "postgres", "postgres": {"url": url}},
            "defaults": {"confidence": {"user_said": 0.7}}, "promotion": {}, "retrieval": {}, "retention": {}}}), encoding="utf-8")
        res = run_memctl(["inspect", "--scope", "agent:a", "--trace-file", str(trace), "--json"],
                         cli_env(home, {"MEMCTL_CONFIG": str(cfg)}), root)
        payload = json.loads(res["stdout"]) if res["exit_code"] == 0 else {}
        out["memctl inspect --scope agent:a --json (store.backend: postgres)"] = {
            "exit_code": res["exit_code"], "history_summary": payload.get("history_summary", "(missing)"),
            "stats.active_record_count": (payload.get("stats") or {}).get("active_record_count"),
            "stderr_tail": [sanitize_text(x).replace(base, "<PGURL>") for x in res["stderr_tail"]] if res["exit_code"] else []}
        res = run_memctl(["inspect", "--scope", "agent:a", "--trace-file", str(trace)], cli_env(home, {"MEMCTL_CONFIG": str(cfg)}), root)
        out["memctl inspect --scope agent:a (human, store.backend: postgres)"] = {
            "exit_code": res["exit_code"], "Deepest history line": "Deepest history" in res["stdout"]}
        store.close()
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
    checks.check("PostgreSQL really ran: the key has two versions on the server",
                 out["history depth for the key (store.history)"] == 2 and out["memory_records rows in the probe schema (server-side count)"] == 4,
                 {k: out[k] for k in ("server", "history depth for the key (store.history)", "memory_records rows in the probe schema (server-side count)")})
    for label in ("bare PostgresMemoryStore", "AuditedMemoryStore(PostgresMemoryStore)"):
        row = out[f"build_inspect_payload, {label}"]
        checks.check(f"PostgreSQL, build_inspect_payload with {label}: no history summary (SQLite-only), stats reported",
                     row["history_summary"] is None and row["stats.active_record_count"] == 3, row)
    row = out["memctl inspect --scope agent:a --json (store.backend: postgres)"]
    checks.check("PostgreSQL, real memctl inspect --json: exit 0, no history summary, stats reported",
                 row["exit_code"] == 0 and row["history_summary"] is None and row["stats.active_record_count"] == 3, row)
    row = out["memctl inspect --scope agent:a (human, store.backend: postgres)"]
    checks.check("PostgreSQL, real memctl inspect (human): exit 0, no Deepest history line",
                 row["exit_code"] == 0 and not row["Deepest history line"], row)
    return out


if __name__ == "__main__":
    sys.exit(main())
