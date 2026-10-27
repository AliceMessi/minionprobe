"""#413 on the installed package: `memctl inspect` with an omitted, empty, blank,
valid or invalid `--scope`.

    python -I -X utf8 probe_413.py <work_dir> <out.json>

The cyclic garbage collector is disabled before openminion is imported and
gc.collect is replaced by a counter that does not collect (0 calls expected),
so only explicit close() and reference counting release a handle (part 4).

Seed (a bare SQLiteMemoryStore, closed before any command): agent:a has two
facts; agent:history has a keyed fact `pref:indent` written twice (put, then
upsert: two versions); session:s1, session:inspect, agent:inspect and
global:system have one fact each; candidates are proposed in session `inspect`
and session `s1`. A trace file holds three events. The records in
session:inspect and agent:inspect make the default inspection identifiers
(`inspect`) visible in the snapshot counts.

1. The real `memctl` console script (one process per command) runs
   `memctl inspect --db <db> --trace-file <trace>` with no `--scope`, with
   `--scope ""` and with `--scope "   "`, each with `--json` and in human mode:
   exit 0; scope null / "scope: all"; the stats equal `memctl stats` without a
   scope (JSON and the human block); snapshot.scope_filter `session:inspect`
   and the session/agent/global/candidate counts of the default identifiers;
   the three spellings give the same output.
2. Valid scopes: `agent:a`, `"  agent:a  "` (padded), `session:s1` and
   `agent:history`, JSON and human: the stats equal `memctl stats --scope`
   of the normalized scope, the snapshot uses the matching identifier, both
   renderers print the same normalized scope, and the padded spelling gives
   the same output as `agent:a`. The deepest keyed history (agent:history
   fact pref:indent, depth 2) is reported through the audited SQLite store memctl
   builds, whatever the scope, and `memctl history` lists both versions.
3. Invalid non-empty scopes (`not-a-scope`, `bogus:x`, `"  not-a-scope  "`),
   JSON and human: exit 1, stdout empty, stderr exactly the CLI error
   `memctl list` / `memctl search` print for the same scope, no traceback.
   On a --db path that does not exist yet, the folder stays empty (no memory or
   audit database is created: the memory service is never opened); the same
   command with a valid scope creates the database (the control).
4. Command lifecycle, in process through typer's CliRunner on the real memctl
   app (this needs counters, so it is not the console script): recording
   subclasses count close() on the service, audit sink and SQLite store that
   memctl's `_get_service` creates, and `_get_service` calls are counted.
   Successful inspects (no scope JSON, blank scope human, padded valid scope
   JSON) and a failing one (build_inspect_payload made to raise) close all
   three once at command exit, leave no sqlite3 connection and the files can
   be renamed and deleted; an invalid scope fails before `_get_service` is
   called. A borrowed service (`_get_service` returning a caller-owned
   service) is not closed by successful, failing or invalid-scope commands and
   stays usable until its owner closes it.
"""

from __future__ import annotations

import gc

gc.disable()  # before openminion is imported; see part 4

import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from collections import Counter  # noqa: E402
from pathlib import Path  # noqa: E402

GC_CALLS = {"n": 0}


def _counting_collect(*_args, **_kwargs):
    GC_CALLS["n"] += 1
    return 0


gc.collect = _counting_collect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize_text, write_result  # noqa: E402

from openminion.modules.memory import cli as memory_cli  # noqa: E402
from openminion.modules.memory.models import MemoryCandidate, MemoryRecord  # noqa: E402
from openminion.modules.memory.service import MemoryService  # noqa: E402
from openminion.modules.memory.storage import (  # noqa: E402
    AuditedMemoryStore,
    SQLiteMemoryAuditSink,
    default_memory_audit_db_path,
)
from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
TRACE_EVENTS = [
    {"ts": "2026-10-01T00:00:01Z", "event": "memory.context.built", "agent_id": "agent:a"},
    {"ts": "2026-10-01T00:00:02Z", "event": "memory.reflection.completed", "agent_id": "agent:a"},
    {"ts": "2026-10-01T00:00:03Z", "event": "memory.retrieval.completed", "agent_id": "agent:a"},
]
DEEPEST = "Deepest history: agent:history fact pref:indent (depth 2)"
HISTORY = {"scope": "agent:history", "type": "fact", "key": "pref:indent", "depth": 2}
NO_SCOPE = {"no --scope": [], '--scope ""': ["--scope", ""], '--scope "   "': ["--scope", "   "]}
MODES = {"--json": ["--json"], "human": []}
# Expected snapshot counts: (scope_filter, session_records, agent_records, global_records, candidate_count)
DEFAULT_SNAPSHOT = ("session:inspect", 1, 1, 1, 1)
VALID = {
    "agent:a": ("agent:a", ("session:inspect", 1, 2, 1, 1), 2),
    "  agent:a  ": ("agent:a", ("session:inspect", 1, 2, 1, 1), 2),
    "session:s1": ("session:s1", ("session:s1", 1, 1, 1, 1), 1),
    "agent:history": ("agent:history", ("session:inspect", 1, 1, 1, 1), 1),
}
INVALID = ["not-a-scope", "bogus:x", "  not-a-scope  "]


def rec(rid: str, scope: str, *, key: str | None = None, title: str = "", content: str = "") -> MemoryRecord:
    return MemoryRecord(id=rid, scope=scope, type="fact", key=key, title=title or rid, content=content or f"{rid} content",
                        created_at="2026-10-01T00:00:00+00:00", updated_at="2026-10-01T00:00:00+00:00")


def seed(db: Path) -> None:
    store = SQLiteMemoryStore(db, artifactctl=None)
    try:
        store.put(rec("a1", "agent:a", title="Prefer tabs"))
        store.put(rec("a2", "agent:a", title="Dark theme"))
        store.put(rec("indent-v1", "agent:history", key="pref:indent", title="Prefer tabs"))
        store.upsert("agent:history", "fact", "pref:indent", {"title": "Prefer spaces", "content": "I prefer spaces."})
        store.put(rec("s1", "session:s1"))
        store.put(rec("si", "session:inspect"))
        store.put(rec("ai", "agent:inspect"))
        store.put(rec("g1", "global:system"))
        for cid, session in (("c-inspect", "inspect"), ("c-s1", "s1")):
            store.candidate_put(MemoryCandidate(candidate_id=cid, session_id=session, proposed_scope=f"session:{session}",
                                                type="fact", content=f"proposed {cid}", status="proposed"))
    finally:
        store.close()


def memctl_exe() -> Path:
    return Path(sys.executable).with_name("memctl.exe" if os.name == "nt" else "memctl")


def cli_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENMINION_", "MEMCTL_"))}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONUTF8="1")
    return env


class Cli:
    def __init__(self, env: dict, cwd: Path) -> None:
        self.env, self.cwd, self.calls = env, cwd, 0

    def __call__(self, *args: str) -> dict:
        self.calls += 1
        proc = subprocess.run([str(memctl_exe()), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env, cwd=str(self.cwd), timeout=600)
        return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def as_json(res: dict):
    if res["exit_code"] != 0:
        return None
    try:
        return json.loads(res["stdout"])
    except json.JSONDecodeError:
        return None


def snap(payload: dict | None):
    s = (payload or {}).get("snapshot") or {}
    return (s.get("scope_filter"), s.get("session_records"), s.get("agent_records"), s.get("global_records"),
            s.get("candidate_count"))


def history(payload: dict | None):
    h = (payload or {}).get("history_summary", "(missing)")
    return {k: h.get(k) for k in ("scope", "type", "key", "depth")} if isinstance(h, dict) else h


def without_timestamp(payload: dict | None):
    if not payload:
        return payload
    out = json.loads(json.dumps(payload))
    out.get("snapshot", {}).pop("snapshot_timestamp", None)
    return out


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def stderr_tail(res: dict, n: int = 3) -> list[str]:
    lines = [ANSI.sub("", x).strip() for x in res["stderr"].strip().splitlines() if x.strip()]
    return [sanitize_text(x) for x in lines[-n:]]


def err_view(text: str) -> str:
    """stderr as stored: in full when short, else its last three lines (ANSI colors removed)."""
    clean = sanitize_text(ANSI.sub("", text))
    if len(clean) <= 300:
        return clean
    return "(last 3 lines) " + " | ".join(stderr_tail({"stderr": text}))


def lines_with(text: str, *needles: str) -> list[str]:
    return [ln for ln in text.splitlines() if any(n in ln for n in needles)]


# ------------------------------------------------------------------ lifecycle helpers
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
    """Counts close() on the services, audit sinks and SQLite stores memctl creates,
    and the calls to memctl's _get_service."""

    def __init__(self) -> None:
        self.services: list = []
        self.closes: Counter = Counter()
        self.get_service_calls = 0
        rec_ = self

        class RecordingService(MemoryService):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                rec_.services.append(self)

            def close(self):
                rec_.closes[id(self)] += 1
                super().close()

        class RecordingSink(SQLiteMemoryAuditSink):
            def close(self):
                rec_.closes[id(self)] += 1
                super().close()

        real_store_close = SQLiteMemoryStore.close
        real_get_service = memory_cli._get_service

        def store_close(store):
            rec_.closes[id(store)] += 1
            real_store_close(store)

        def counting_get_service(*a, **k):
            rec_.get_service_calls += 1
            return real_get_service(*a, **k)

        self._saved = (memory_cli.MemoryService, memory_cli.SQLiteMemoryAuditSink, real_store_close, real_get_service)
        memory_cli.MemoryService = RecordingService
        memory_cli.SQLiteMemoryAuditSink = RecordingSink
        SQLiteMemoryStore.close = store_close
        memory_cli._get_service = counting_get_service

    def restore(self) -> None:
        (memory_cli.MemoryService, memory_cli.SQLiteMemoryAuditSink, SQLiteMemoryStore.close,
         memory_cli._get_service) = self._saved

    def counts(self, service) -> list[int]:
        audited = service._store  # noqa: SLF001
        return [self.closes[id(service)], self.closes[id(audited._sink)], self.closes[id(audited._store)]]  # noqa: SLF001


def invoke(args: list[str]):
    from typer.testing import CliRunner

    res = CliRunner().invoke(memory_cli._build_app(), args)
    exc = res.exception
    try:
        err = res.stderr
    except (ValueError, AttributeError):
        err = "(stderr not captured separately)"
    return res, {"exit_code": res.exit_code, "stdout_is_empty": res.stdout == "",
                 "exception": None if exc is None else f"{type(exc).__name__}: {exc}",
                 "stderr": err_view(err)}


def main() -> int:
    quiet()
    clear_override_env()
    info = banner("probe_413", gc_enabled=gc.isenabled(), memctl=str(memctl_exe()), memctl_exists=memctl_exe().exists())
    checks = Checks()
    results: dict = {}
    root = WORK / "run"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    home = root / "home"
    home.mkdir()
    cli = Cli(cli_env(home), root)

    data = root / "data"
    data.mkdir()
    db = data / "memory.db"
    trace = data / "trace.jsonl"
    trace.write_text("".join(json.dumps(e) + "\n" for e in TRACE_EVENTS), encoding="utf-8")
    seed(db)
    common = ["--db", str(db), "--trace-file", str(trace)]

    # memctl stats controls (scope-free and per scope).
    stats_json = {None: as_json(cli("stats", "--db", str(db), "--json"))}
    stats_human_all = cli("stats", "--db", str(db))["stdout"].strip()
    for scope in {v[0] for v in VALID.values()}:
        stats_json[scope] = as_json(cli("stats", "--scope", scope, "--db", str(db), "--json"))
    stats_human_a = cli("stats", "--scope", "agent:a", "--db", str(db))["stdout"].strip()
    results["memctl stats --json (no scope / per scope)"] = {str(k): v for k, v in stats_json.items()}
    checks.check("control: memctl stats --json without a scope reports every scope (7 active records, 1 supersession chain)",
                 (stats_json[None] or {}).get("active_record_count") == 7
                 and (stats_json[None] or {}).get("supersession_chain_count") == 1, stats_json[None])

    # 1. No scope, empty, blank.
    part1: dict = {}
    outputs: dict = {"--json": [], "human": []}
    for label, scope_args in NO_SCOPE.items():
        for mode, mode_args in MODES.items():
            res = cli("inspect", *scope_args, *common, *mode_args)
            name = f"memctl inspect {label} {mode}"
            if mode == "--json":
                payload = as_json(res)
                row = {"exit_code": res["exit_code"], "scope": (payload or {}).get("scope", "(missing)"),
                       "stats.scope": ((payload or {}).get("stats") or {}).get("scope", "(missing)"),
                       "stats equal memctl stats --json (no scope)": payload is not None and payload.get("stats") == stats_json[None],
                       "stats.active_record_count": ((payload or {}).get("stats") or {}).get("active_record_count"),
                       "snapshot (scope_filter, session, agent, global, candidates)": snap(payload),
                       "history_summary": history(payload),
                       "recent_trace_events": [e.get("event") for e in (payload or {}).get("recent_trace_events", [])],
                       "stderr_tail": stderr_tail(res)}
                outputs[mode].append(without_timestamp(payload))
                ok = (res["exit_code"] == 0 and row["scope"] is None and row["stats.scope"] is None
                      and row["stats equal memctl stats --json (no scope)"] and row["stats.active_record_count"] == 7
                      and row["snapshot (scope_filter, session, agent, global, candidates)"] == DEFAULT_SNAPSHOT
                      and row["history_summary"] == HISTORY and len(row["recent_trace_events"]) == 3)
                checks.check(f"1. {name}: exit 0, scope null, all-scope stats (= memctl stats), default `inspect` identifiers, history and trace kept",
                             ok, row)
            else:
                out = res["stdout"]
                row = {"exit_code": res["exit_code"], "first line": (out.splitlines() or [""])[0],
                       "scope lines": lines_with(out, "scope:", "Scope:"),
                       "contains the memctl stats block (no scope)": bool(stats_human_all) and stats_human_all in out,
                       "Deepest history line": lines_with(out, "Deepest history"),
                       "trace lines": lines_with(out, "Last GC-ish", "Last reflection", "Recent trace events"),
                       "stderr_tail": stderr_tail(res)}
                outputs[mode].append(out)
                ok = (res["exit_code"] == 0 and row["first line"] == "Memory inspect"
                      and row["scope lines"] == ["  scope: all", "Scope: all"]
                      and row["contains the memctl stats block (no scope)"] and row["Deepest history line"] == [DEEPEST]
                      and len(row["trace lines"]) == 3)
                checks.check(f"1. {name}: exit 0, 'scope: all', the all-scope stats block of memctl stats, history and trace kept",
                             ok, row)
            part1[name] = row
    for mode in MODES:
        same = len(outputs[mode]) == 3 and all(o == outputs[mode][0] for o in outputs[mode]) and bool(outputs[mode][0])
        checks.check(f"1. {mode}: no --scope, --scope \"\" and --scope \"   \" give the same output"
                     + (" (snapshot timestamp aside)" if mode == "--json" else ""), same,
                     {"identical": same, "count": len(outputs[mode])})
    results["1. no / empty / blank scope"] = part1
    results["1. human output, no --scope"] = sanitize_text(outputs["human"][0] or "")

    # 2. Valid scopes.
    part2: dict = {}
    keep: dict = {}
    for raw, (norm, want_snap, want_active) in VALID.items():
        rj = cli("inspect", "--scope", raw, *common, "--json")
        pj = as_json(rj)
        rowj = {"exit_code": rj["exit_code"], "scope": (pj or {}).get("scope", "(missing)"),
                "stats.scope": ((pj or {}).get("stats") or {}).get("scope", "(missing)"),
                f"stats equal memctl stats --scope {norm} --json": pj is not None and pj.get("stats") == stats_json.get(norm),
                "stats.active_record_count": ((pj or {}).get("stats") or {}).get("active_record_count"),
                "snapshot (scope_filter, session, agent, global, candidates)": snap(pj),
                "history_summary": history(pj), "stderr_tail": stderr_tail(rj)}
        part2[f"memctl inspect --scope {raw!r} --json"] = rowj
        keep[(raw, "--json")] = without_timestamp(pj)
        checks.check(f"2. memctl inspect --scope {raw!r} --json: scope {norm!r}, stats of {norm} (= memctl stats --scope), "
                     f"snapshot {want_snap[0]}, keyed history depth 2",
                     rj["exit_code"] == 0 and rowj["scope"] == norm and rowj["stats.scope"] == norm
                     and rowj[f"stats equal memctl stats --scope {norm} --json"] and rowj["stats.active_record_count"] == want_active
                     and rowj["snapshot (scope_filter, session, agent, global, candidates)"] == want_snap
                     and rowj["history_summary"] == HISTORY, rowj)
        if raw == "agent:history":
            st = (pj or {}).get("stats") or {}
            checks.check("2. --scope agent:history --json: the keyed history shows in the scoped stats (1 chain, max depth 2)",
                         st.get("supersession_chain_count") == 1 and st.get("max_chain_depth") == 2,
                         {k: st.get(k) for k in ("supersession_chain_count", "max_chain_depth", "active_record_count")})
            continue
        rh = cli("inspect", "--scope", raw, *common)
        out = rh["stdout"]
        rowh = {"exit_code": rh["exit_code"], "scope lines": lines_with(out, "scope:", "Scope:"),
                "Active records line": lines_with(out, "Active records:"),
                "Deepest history line": lines_with(out, "Deepest history"), "stderr_tail": stderr_tail(rh)}
        if norm == "agent:a":
            rowh["contains the memctl stats --scope agent:a block"] = bool(stats_human_a) and stats_human_a in out
        part2[f"memctl inspect --scope {raw!r} (human)"] = rowh
        keep[(raw, "human")] = out
        checks.check(f"2. memctl inspect --scope {raw!r} (human): 'scope: {norm}' and 'Scope: {norm}' (same normalized scope as JSON), "
                     f"{want_active} active, keyed history depth 2",
                     rh["exit_code"] == 0 and rowh["scope lines"] == [f"  scope: {norm}", f"Scope: {norm}"]
                     and rowh["Active records line"] == [f"Active records: {want_active}"]
                     and rowh["Deepest history line"] == [DEEPEST]
                     and rowh.get("contains the memctl stats --scope agent:a block", True), rowh)
    for mode in MODES:
        same = keep.get(("  agent:a  ", mode)) == keep.get(("agent:a", mode)) and bool(keep.get(("agent:a", mode)))
        checks.check(f"2. {mode}: --scope \"  agent:a  \" gives the same output as --scope agent:a", same, {"identical": same})
    res = cli("history", "--scope", "agent:history", "--type", "fact", "--key", "pref:indent", "--db", str(db), "--json")
    titles = [r.get("title") for r in (as_json(res) or [])]
    part2["memctl history --scope agent:history --type fact --key pref:indent --json"] = {"exit_code": res["exit_code"], "titles": titles}
    checks.check("2. memctl history (audited store) lists both versions of the keyed fact",
                 res["exit_code"] == 0 and sorted(titles) == ["Prefer spaces", "Prefer tabs"], titles)
    results["2. valid scopes"] = part2
    # Observation only, outside #413 (no check): memctl stats with the padded scope.
    padded = as_json(cli("stats", "--scope", "  agent:a  ", "--db", str(db), "--json")) or {}
    results["observation (not a check): memctl stats --scope '  agent:a  ' --json"] = {
        k: padded.get(k) for k in ("scope", "active_record_count")}

    # 3. Invalid non-empty scopes.
    part3: dict = {}
    for raw in INVALID:
        want = f"Error: invalid scope: '{raw.strip()}'\n"
        listed = cli("list", "--scope", raw.strip(), "--db", str(db))
        for mode, mode_args in MODES.items():
            res = cli("inspect", "--scope", raw, *common, *mode_args)
            row = {"exit_code": res["exit_code"], "stdout": res["stdout"], "stderr": err_view(res["stderr"]),
                   "traceback in stderr": "Traceback" in res["stderr"],
                   "memctl list --scope <same scope, stripped>: (exit, stderr)": [listed["exit_code"], err_view(listed["stderr"])]}
            part3[f"memctl inspect --scope {raw!r} {mode}"] = row
            checks.check(f"3. memctl inspect --scope {raw!r} {mode}: exit 1, stdout empty, stderr exactly {want.strip()!r} "
                         "(as memctl list), no traceback",
                         res["exit_code"] == 1 and res["stdout"] == "" and res["stderr"] == want and not row["traceback in stderr"]
                         and listed["exit_code"] == 1 and listed["stderr"] == want, row)
    searched = cli("search", "tabs", "--scope", "not-a-scope", "--db", str(db))
    part3["memctl search tabs --scope 'not-a-scope' (existing presentation)"] = {"exit_code": searched["exit_code"],
                                                                                "stderr": err_view(searched["stderr"])}
    checks.check("3. control: memctl search --scope not-a-scope prints the same error (exit 1)",
                 searched["exit_code"] == 1 and searched["stderr"] == "Error: invalid scope: 'not-a-scope'\n",
                 part3["memctl search tabs --scope 'not-a-scope' (existing presentation)"])
    for label, scope in (("invalid scope", "not-a-scope"), ("valid scope (control)", "agent:a")):
        folder = root / ("fresh-invalid" if scope == "not-a-scope" else "fresh-valid")
        folder.mkdir()
        target = folder / "memory.db"
        rows = {}
        for mode, mode_args in MODES.items():
            res = cli("inspect", "--scope", scope, "--db", str(target), *mode_args)
            rows[mode] = {"exit_code": res["exit_code"], "stderr_tail": stderr_tail(res, 1)}
        rows["files in the folder after the commands"] = sorted(p.name for p in folder.iterdir())
        part3[f"memctl inspect --scope {scope} --db <folder>/memory.db (not yet created)"] = rows
        if scope == "not-a-scope":
            checks.check("3. invalid scope on a --db that does not exist: exit 1 and no database or audit database created "
                         "(the memory service is never opened)",
                         all(rows[m]["exit_code"] == 1 for m in MODES) and rows["files in the folder after the commands"] == [], rows)
        else:
            checks.check("3. control: the same command with a valid scope opens the service and creates the database",
                         all(rows[m]["exit_code"] == 0 for m in MODES) and "memory.db" in rows["files in the folder after the commands"],
                         rows)
    results["3. invalid scopes"] = part3
    results["memctl processes run"] = cli.calls

    # 4. Command lifecycle in process (CliRunner), GC disabled.
    part4: dict = {}
    life_cases = [
        ("successful inspect, no --scope, --json", [], ["--json"], None),
        ("successful inspect, --scope \"   \", human", ["--scope", "   "], [], None),
        ("successful inspect, --scope \"  agent:a  \", --json", ["--scope", "  agent:a  "], ["--json"], None),
        ("failing inspect, invalid --scope not-a-scope", ["--scope", "not-a-scope"], ["--json"], "invalid"),
        ("failing inspect, build_inspect_payload raises (no --scope)", [], ["--json"], "raise"),
    ]
    for index, (label, scope_args, mode_args, failure) in enumerate(life_cases):
        folder = root / f"life-{index}"
        folder.mkdir()
        path = folder / "memory.db"
        seed(path)
        recorder = Recorder()
        saved_payload = memory_cli.build_inspect_payload
        if failure == "raise":
            def boom(**_kwargs):
                raise RuntimeError("inspect failed")
            memory_cli.build_inspect_payload = boom
        try:
            res, row = invoke(["inspect", *scope_args, "--db", str(path), "--trace-file", str(trace), *mode_args])
        finally:
            memory_cli.build_inspect_payload = saved_payload
            recorder.restore()
        row["_get_service calls"] = recorder.get_service_calls
        row["services created"] = len(recorder.services)
        if recorder.services:
            service = recorder.services[0]
            row["close calls after the command (service, sink, store)"] = recorder.counts(service)
        row["open connections to the database or audit database"] = open_connections(path, default_memory_audit_db_path(path))
        row["gc enabled"] = gc.isenabled()
        row["files right after the command"] = rename_and_delete(folder)
        if recorder.services:
            recorder.services[0].close()
            row["close calls after a second service.close() (service, sink, store)"] = recorder.counts(recorder.services[0])
        part4[label] = row
        files_ok = bool(row["files right after the command"]) and all(
            v == "renamed and deleted" for v in row["files right after the command"].values())
        no_conn = row["open connections to the database or audit database"] == []
        if failure == "invalid":
            ok = (row["exit_code"] == 1 and (row["exception"] or "").startswith("SystemExit")
                  and row["stderr"] == "Error: invalid scope: 'not-a-scope'\n" and row["stdout_is_empty"]
                  and row["_get_service calls"] == 0 and row["services created"] == 0 and no_conn and files_ok)
            checks.check(f"4. in process, GC disabled, {label}: exit 1 before _get_service is called (no service, store or "
                         "audit sink created); no open connection; files renamed and deleted", ok, row)
            continue
        if failure == "raise":
            ok_exit = row["exit_code"] == 1 and (row["exception"] or "") == "RuntimeError: inspect failed"
        else:
            ok_exit = row["exit_code"] == 0 and row["exception"] is None
        ok = (ok_exit and row["_get_service calls"] == 1 and row["services created"] == 1
              and row.get("close calls after the command (service, sink, store)") == [1, 1, 1] and no_conn and files_ok
              and row.get("close calls after a second service.close() (service, sink, store)") == [2, 1, 1])
        checks.check(f"4. in process, GC disabled, {label}: the command-owned service, audit sink and store closed once at "
                     "command exit; no open connection; files renamed and deleted; a second close is a no-op", ok, row)

    # Borrowed service.
    closes: Counter = Counter()

    class BorrowedStore(SQLiteMemoryStore):
        def close(self):
            closes["store"] += 1
            super().close()

    class BorrowedSink(SQLiteMemoryAuditSink):
        def close(self):
            closes["sink"] += 1
            super().close()

    class BorrowedService(MemoryService):
        def close(self):
            closes["service"] += 1
            super().close()

    folder = root / "borrowed"
    folder.mkdir()
    path = folder / "memory.db"
    seed(path)
    store = BorrowedStore(path, artifactctl=None)
    sink = BorrowedSink(default_memory_audit_db_path(path))
    borrowed = BorrowedService(AuditedMemoryStore(store, sink=sink))
    factory_calls = {"n": 0}

    def factory(*_a, **_k):
        factory_calls["n"] += 1
        return borrowed

    borrow: dict = {}
    saved = memory_cli._get_service
    memory_cli._get_service = factory
    try:
        for label, scope_args, failure in (("successful inspect, no --scope", [], None),
                                           ("failing inspect (build_inspect_payload raises)", [], "raise"),
                                           ("failing inspect, invalid --scope", ["--scope", "not-a-scope"], None)):
            before = factory_calls["n"]
            saved_payload = memory_cli.build_inspect_payload
            if failure == "raise":
                def boom(**_kwargs):
                    raise RuntimeError("inspect failed")
                memory_cli.build_inspect_payload = boom
            try:
                _res, row = invoke(["inspect", *scope_args, "--db", str(path), "--trace-file", str(trace), "--json"])
            finally:
                memory_cli.build_inspect_payload = saved_payload
            row["factory calls"] = factory_calls["n"] - before
            row["close calls on the borrowed service, sink, store"] = [closes["service"], closes["sink"], closes["store"]]
            got = borrowed._store.get("a1")  # noqa: SLF001
            row["borrowed service still usable (get a1)"] = None if got is None else got.title
            borrow[label] = row
    finally:
        memory_cli._get_service = saved
    store_before_owner = [closes["service"], closes["sink"], closes["store"]]
    sink.close()
    store.close()
    borrow["owner closes the sink and store: close calls (service, sink, store)"] = [closes["service"], closes["sink"], closes["store"]]
    borrow["open connections after the owner closes"] = open_connections(path, default_memory_audit_db_path(path))
    borrow["files after the owner closes"] = rename_and_delete(folder)
    part4["borrowed service (memctl's _get_service returns a caller-owned service)"] = borrow
    rows = [borrow[k] for k in ("successful inspect, no --scope", "failing inspect (build_inspect_payload raises)",
                                "failing inspect, invalid --scope")]
    ok = (rows[0]["exit_code"] == 0 and rows[0]["factory calls"] == 1
          and rows[1]["exit_code"] == 1 and rows[1]["factory calls"] == 1
          and rows[2]["exit_code"] == 1 and rows[2]["factory calls"] == 0
          and all(r["close calls on the borrowed service, sink, store"] == [0, 0, 0]
                  and r["borrowed service still usable (get a1)"] == "Prefer tabs" for r in rows)
          and store_before_owner == [0, 0, 0]
          and borrow["owner closes the sink and store: close calls (service, sink, store)"] == [0, 1, 1]
          and borrow["open connections after the owner closes"] == []
          and all(v == "renamed and deleted" for v in borrow["files after the owner closes"].values()))
    checks.check("4. in process, GC disabled: a borrowed service is not closed by successful, failing or invalid-scope "
                 "commands (invalid scope never asks for it), stays usable, and its owner closes it", ok, borrow)
    results["4. command lifecycle (in process, cyclic GC disabled)"] = part4

    checks.check("cyclic GC stayed disabled and gc.collect was never called", not gc.isenabled() and GC_CALLS["n"] == 0,
                 {"gc_enabled": gc.isenabled(), "gc.collect calls": GC_CALLS["n"]})
    summary = checks.summary()
    write_result(OUT, {"info": info, "results": results, "checks": checks.items, "summary": summary})
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
