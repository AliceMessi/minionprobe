"""#396 on the installed package: run readers select a case-sensitive `run.` prefix.

    python -I probe_396.py <work> <out.json>

SQLite always; PostgreSQL too when OPENMINION_TEST_POSTGRES_URL is set (a new
schema, dropped at the end). Every write goes through the runtime SessionStore
(`append_event`, the store method behind the generic event append API, which
keeps event types as given): a real APIRuntime's on SQLite; on PostgreSQL the
one build_runtime_storage builds, because APIRuntime does not start on
PostgreSQL runtime storage (also on 0.1.11). Reads go through the run readers
(`list_session_runs`, `list_session_run_events`), the runs API
(`openminion.api.queries.runs`), on SQLite the `openminion status runs` /
`status run-events` CLI, and the generic `list_events` / `count_events`.
Expected values are written out here or computed by an oracle in this file
from all of a session's events (type starts with lowercase `run.`), not by the
store. Runs on 0.1.12 and, as the negative control, on 0.1.11.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402
import probe_369_lib as L  # noqa: E402

from openminion.api.queries.runs import RunQueryError, list_run_events, list_runs  # noqa: E402
from openminion.modules.task.run import (  # noqa: E402
    list_session_run_events,
    list_session_runs,
    resolve_invocation_terminal,
    resolve_thread_lifecycle,
)

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2])
PGURL = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()
TAG = uuid.uuid4().hex[:8]
EVENT_CLOCK = "openminion.modules.storage.runtime.session_store.lifecycle.utc_now_iso"
STATES = {"queued", "running", "waiting_tool", "responding", "completed", "failed", "cancelled"}
TERMINAL = {"completed", "failed", "cancelled"}

quiet()
clear_override_env()
WORK.mkdir(parents=True, exist_ok=True)
checks = Checks()
info = banner("probe_396")
results: dict[str, Any] = {}


# ---------------------------------------------------------------- oracle
def oracle_state(event_type: str, payload: dict[str, Any]) -> str:
    candidate = str(payload.get("state") or "").strip().lower()
    if candidate in STATES:
        return candidate
    if event_type.startswith("run."):
        suffix = event_type.split(".", 1)[1].strip().lower()
        if suffix in STATES:
            return suffix
    return "running"


def run_events_oracle(sessions, sid: str) -> list:
    events = sessions.list_events(session_id=sid, limit=1_000_000)
    return [e for e in events if e.event_type.startswith("run.") and str(e.payload.get("run_id", "")).strip()]


def runs_oracle(sessions, sid: str, limit: int) -> list[tuple]:
    grouped: dict[str, list] = {}
    for e in run_events_oracle(sessions, sid):
        grouped.setdefault(str(e.payload["run_id"]).strip(), []).append(e)
    out = []
    for run_id, evs in grouped.items():
        evs.sort(key=lambda e: int(e.id))
        last = evs[-1]
        step = next((str(e.payload.get("step")).strip() for e in reversed(evs) if str(e.payload.get("step", "")).strip()), "")
        error = next((str(e.payload.get("error")).strip() for e in reversed(evs)
                      if e.payload.get("error") is not None and str(e.payload.get("error")).strip()), "")
        terminal = next((e.created_at for e in reversed(evs) if oracle_state(e.event_type, e.payload) in TERMINAL), "")
        out.append((evs[0].created_at, run_id, oracle_state(last.event_type, last.payload), step, terminal, len(evs), error))
    out.sort(key=lambda r: (str(r[0]), str(r[1])), reverse=True)
    return [(r[1], r[2], r[3], r[0], r[4], r[5], r[6]) for r in out[:limit]]


def summary_tuple(run) -> tuple:
    return (run.run_id, run.state, run.current_step, run.started_at, run.ended_at, run.event_count, run.error)


def append(sessions, sid: str, event_type: str, **payload: Any):
    return sessions.append_event(session_id=sid, event_type=event_type, payload=payload)


@contextmanager
def fixed_clock(stamp: str = "2026-10-26T09:00:00.000001+00:00") -> Iterator[None]:
    with patch(EVENT_CLOCK, return_value=stamp):
        yield


def count_rows(read) -> tuple[Any, int]:
    from openminion.modules.storage.runtime.session_store.backend import RuntimeSessionStoreBackend

    seen: list[int] = []
    real = RuntimeSessionStoreBackend.query_dicts

    def counting(self, sql, params=None):
        rows = real(self, sql, params)
        seen.append(len(rows))
        return rows

    with patch.object(RuntimeSessionStoreBackend, "query_dicts", counting):
        value = read()
    return value, sum(seen)


# ---------------------------------------------------------------- scenario per backend
def scenario(backend: str, rt, root: Path, config: Path) -> None:
    s = rt.sessions
    B = backend

    def new_session(name: str) -> str:
        sid = f"{name}-{TAG}"
        s.resolve_session(agent_id="openminion", channel="console", target="probe-396", session_id=sid,
                          metadata={"surface": "acp", "probe": "396", "name": name})
        return sid

    # 1. The #396 report events plus a lowercase-prefix/upper-suffix run.
    sid = new_session("report")
    ids = {"conversation_id": "conv", "thread_id": "t"}
    events = (
        ("run.queued", {"run_id": "r1", "state": "queued", "invocation_id": "inv1"}),
        ("run.running", {"run_id": "r1"}),
        ("RUN.failed", {"run_id": "r1", "state": "failed", "error": "boom"}),
        ("run.queued", {"run_id": "r2", "state": "queued"}),
        ("Run.completed", {"run_id": "r2"}),
        ("run.COMPLETED", {"run_id": "r3", "thread_id": "u"}),
    )
    written = [s.append_event(session_id=sid, event_type=t, payload={**ids, **p}) for t, p in events]
    stored = [e.event_type for e in s.list_events(session_id=sid, limit=100)]
    checks.check(f"{B}: stored event types are kept as given", stored == [t for t, _ in events], observed=stored)
    runs = [(r.run_id, r.state, r.event_count, bool(r.ended_at), r.error) for r in list_session_runs(s, session_id=sid)]
    results[f"{B} report runs"] = runs
    checks.check(f"{B}: list_session_runs counts only lowercase run. events",
                 runs == [("r3", "completed", 1, True, ""), ("r2", "queued", 1, False, ""), ("r1", "running", 2, False, "")],
                 observed=runs)
    per_run = {rid: [(e.id, e.event_type, e.state) for e in list_session_run_events(s, session_id=sid, run_id=rid)] for rid in ("r1", "r2", "r3")}
    results[f"{B} report run events"] = per_run
    checks.check(f"{B}: exact-run reads list only lowercase run. events (suffix any case)",
                 per_run == {"r1": [(written[0].id, "run.queued", "queued"), (written[1].id, "run.running", "running")],
                             "r2": [(written[3].id, "run.queued", "queued")],
                             "r3": [(written[5].id, "run.COMPLETED", "completed")]}, observed=per_run)
    proj = resolve_thread_lifecycle(s, session_id=sid, **ids)
    term = resolve_invocation_terminal(s, session_id=sid, trigger_event=written[2], **ids)
    checks.check(f"{B}: thread projection and invocation terminal agree (r2 queued; RUN.failed is no terminal)",
                 (proj.latest_run_id, proj.latest_run_state) == ("r2", "queued") and term is None,
                 observed=((proj.latest_run_id, proj.latest_run_state), term))

    # Runs API (developer API query helpers) on the same runtime.
    api_runs = [(r["run_id"], r["state"], r["event_count"]) for r in list_runs(None, session_id=sid, runtime=rt)["runs"]]
    checks.check(f"{B}: runs API list_runs matches", api_runs == [("r3", "completed", 1), ("r2", "queued", 1), ("r1", "running", 2)], observed=api_runs)
    api_ev = [e["event_type"] for e in list_run_events(None, session_id=sid, run_id="r1", runtime=rt)["events"]]
    checks.check(f"{B}: runs API list_run_events r1 = run.queued, run.running", api_ev == ["run.queued", "run.running"], observed=api_ev)
    append(s, sid, "RUN.queued", run_id="upper-only", **ids)
    try:
        upper = list_run_events(None, session_id=sid, run_id="upper-only", runtime=rt)
        upper_out = f"found {[e['event_type'] for e in upper['events']]}"
    except RunQueryError as exc:
        upper_out = f"RunQueryError {exc.code}"
    checks.check(f"{B}: a run with only an upper-case RUN. event is not a run (runs API: run_not_found)",
                 upper_out == "RunQueryError run_not_found"
                 and "upper-only" not in [r.run_id for r in list_session_runs(s, session_id=sid)], observed=upper_out)

    # Generic event readers keep their own prefix semantics (unchanged by #396).
    gen_list = len(s.list_events(session_id=sid, limit=100, event_type_prefix="run."))
    gen_count = s.count_events(session_id=sid, event_type_prefix="run.")
    expected_generic = 7 if B == "sqlite" else 4  # SQLite LIKE ignores ASCII case; PostgreSQL LIKE does not
    results[f"{B} generic"] = (gen_list, gen_count)
    checks.check(f"{B}: generic list_events/count_events(event_type_prefix='run.') unchanged ({expected_generic}/{expected_generic})",
                 (gen_list, gen_count) == (expected_generic, expected_generic), observed=(gen_list, gen_count))

    if B == "sqlite":
        exe = L.cli_exe()
        base = [exe, "--config", str(config), "--home-root", str(root), "--data-root", str(root / ".openminion"), "status"]
        p1 = subprocess.run(base + ["runs", "--session-id", sid, "--json"], capture_output=True, text=True, env=L.clean_env(), cwd=str(root), timeout=300)
        p2 = subprocess.run(base + ["run-events", "--session-id", sid, "--run-id", "r1", "--json"], capture_output=True, text=True,
                            env=L.clean_env(), cwd=str(root), timeout=300)
        try:
            cli_runs = [(r["run_id"], r["state"], r["event_count"]) for r in json.loads(p1.stdout)["runs"]]
            cli_ev = [e["event_type"] for e in json.loads(p2.stdout)["events"]]
        except Exception as exc:  # noqa: BLE001
            cli_runs, cli_ev = f"{type(exc).__name__}: {p1.stdout[-300:]} {p1.stderr[-300:]}", None
        checks.check("sqlite: `openminion status runs --json` (installed CLI) matches",
                     cli_runs == [("r3", "completed", 1), ("r2", "queued", 1), ("r1", "running", 2)], observed=cli_runs)
        checks.check("sqlite: `openminion status run-events --run-id r1 --json` = run.queued, run.running",
                     cli_ev == ["run.queued", "run.running"], observed=cli_ev)

    # 2. Mixed case variants of the same run ids.
    sid = new_session("mixed")
    append(s, sid, "run.queued", run_id="m1", state="queued", step="accepted")
    append(s, sid, "RUN.running", run_id="m1", state="running", step="upper-step")
    append(s, sid, "Run.failed", run_id="m1", state="failed", error="upper-error")
    append(s, sid, "run.running", run_id="m1", step="s1")
    append(s, sid, "RUN.completed", run_id="m1", state="completed")
    append(s, sid, "Run.queued", run_id="m2", state="queued")
    append(s, sid, "run.completed", run_id="m2", state="completed")
    got = [summary_tuple(r) for r in list_session_runs(s, session_id=sid)]
    exp = runs_oracle(s, sid, 20)
    results[f"{B} mixed runs"] = got
    checks.check(f"{B}: same run ids with run./RUN./Run.: state/step/start/end/count/error from lowercase events only (oracle)",
                 got == exp, observed=got)
    brief = sorted((g[0], g[1], g[2], g[5], bool(g[4]), g[6]) for g in got)
    checks.check(f"{B}: m1 = running, step s1, 2 events, not ended, no error; m2 = completed, 1 event, ended",
                 brief == [("m1", "running", "s1", 2, False, ""), ("m2", "completed", "", 1, True, "")], observed=brief)
    m1 = [(e.event_type, e.state) for e in list_session_run_events(s, session_id=sid, run_id="m1")]
    checks.check(f"{B}: m1 exact read = run.queued, run.running", m1 == [("run.queued", "queued"), ("run.running", "running")], observed=m1)

    # 3. Suffix handling.
    sid = new_session("suffix")
    for t, rid, extra in (("run.COMPLETED", "x1", {}), ("run.Failed", "x2", {}), ("run.WAITING_TOOL", "x3", {}),
                          ("run.Cancelled", "x4", {}), ("run.queued", "x5", {"state": "completed"}), ("run.bogus", "x6", {}),
                          ("run.failed", "x7", {"state": " RUNNING "})):
        append(s, sid, t, run_id=rid, **extra)
    states = {r.run_id: r.state for r in list_session_runs(s, session_id=sid)}
    checks.check(f"{B}: state from the suffix in any case; payload state wins; unknown suffix -> running",
                 states == {"x1": "completed", "x2": "failed", "x3": "waiting_tool", "x4": "cancelled", "x5": "completed",
                            "x6": "running", "x7": "running"}, observed=states)

    # 4. Facts and tied ordering over an interleaved history at one timestamp.
    sid = new_session("ties")
    variants = ("run.", "RUN.", "Run.", "run.", "rUN.")
    with fixed_clock():
        for i in range(60):
            rid = f"t{i % 9}"
            prefix = variants[i % len(variants)]
            state = ("queued", "running", "waiting_tool", "completed", "failed")[i % 5]
            payload: dict[str, Any] = {"run_id": rid}
            if i % 2:
                payload["state"] = state
            if i % 7 == 0:
                payload["step"] = f"step-{i}"
            if i % 11 == 0:
                payload["error"] = f"err-{i}"
            s.append_event(session_id=sid, event_type=prefix + state, payload=payload)
    got = [summary_tuple(r) for r in list_session_runs(s, session_id=sid, limit=20)]
    exp = runs_oracle(s, sid, 20)
    results[f"{B} ties runs"] = got
    checks.check(f"{B}: 60 interleaved events at one timestamp: summaries = oracle (state/step/start/end/count/error), ties by run_id desc",
                 got == exp and [g[0] for g in got] == sorted({g[0] for g in got}, reverse=True), observed=[g[:3] + g[5:] for g in got])
    t0 = [int(e.id) for e in list_session_run_events(s, session_id=sid, run_id="t0", limit=1000)]
    t0_exp = [int(e.id) for e in run_events_oracle(s, sid) if str(e.payload["run_id"]) == "t0"]
    checks.check(f"{B}: tied exact-run read = lowercase events in id order", t0 == sorted(t0_exp), observed=len(t0))

    # 5. Bounded rows: limits and a scan window full of upper-case events.
    bounded = [summary_tuple(r) for r in list_session_runs(s, session_id=sid, limit=2)]
    checks.check(f"{B}: list_session_runs(limit=2) returns the 2 newest of the oracle", bounded == exp[:2], observed=[b[0] for b in bounded])
    last3 = [int(e.id) for e in list_session_run_events(s, session_id=sid, run_id="t0", limit=3)]
    checks.check(f"{B}: list_session_run_events(limit=3) returns the newest 3 lowercase events, oldest first", last3 == sorted(t0_exp)[-3:], observed=last3)
    sid = new_session("window")
    for state in ("queued", "running", "completed"):
        append(s, sid, f"run.{state}", run_id="early", state=state)
    for i in range(50):
        append(s, sid, "RUN.running", run_id="noise", state="running", step=f"n{i}")
    (small, rows_small) = count_rows(lambda: [r.run_id for r in list_session_runs(s, session_id=sid, limit=5, scan_limit=10)])
    (ev_small, rows_ev_small) = count_rows(lambda: [e.event_type for e in list_session_run_events(s, session_id=sid, run_id="early", limit=5)])
    checks.check(f"{B}: a scan window of 10 after 50 newer RUN. events still finds the lowercase run",
                 small == ["early"], observed=small)
    for i in range(450):
        append(s, sid, "Run.running", run_id="noise", state="running")
    (big, rows_big) = count_rows(lambda: [r.run_id for r in list_session_runs(s, session_id=sid, limit=5, scan_limit=10)])
    (ev_big, rows_ev_big) = count_rows(lambda: [e.event_type for e in list_session_run_events(s, session_id=sid, run_id="early", limit=5)])
    results[f"{B} rows read"] = {"runs 50": rows_small, "runs 500": rows_big, "events 50": rows_ev_small, "events 500": rows_ev_big}
    checks.check(f"{B}: rows read by the run readers do not grow with 50 -> 500 upper-case events",
                 big == ["early"] and ev_big == ev_small == ["run.queued", "run.running", "run.completed"]
                 and rows_big == rows_small and rows_ev_big == rows_ev_small,
                 observed=results[f"{B} rows read"])

    # 6. Migration-15 metadata filters still select these sessions.
    listed = sorted(r.id for r in s.list_sessions(limit=50, target="probe-396", metadata_filter={"probe": "396"}))
    expected_ids = sorted(f"{n}-{TAG}" for n in ("report", "mixed", "suffix", "ties", "window"))
    checks.check(f"{B}: metadata filter {{'probe': '396'}} lists the 5 sessions after the run events",
                 listed == expected_ids, observed=listed)
    if L.IS_0112:
        storage_rows = {r for r in L.stored_rows(_Facade(s)) if r[0].endswith(TAG)}
        meta = {k: v for k, v in L.stored_metadata(_Facade(s)).items() if k.endswith(TAG)}
        checks.check(f"{B}: session_metadata_strings rows of these sessions = their stored metadata",
                     storage_rows == L.expected_rows(meta), observed=len(storage_rows))


class _Facade:
    """Lets probe_369_lib.q() read through a SessionStore."""

    def __init__(self, sessions) -> None:
        self.sessions = sessions


def run_backend(backend: str) -> None:
    root = WORK / f"{backend}-{TAG}"
    schema = None
    url = None
    if backend == "postgres":
        schema = f"om396_{TAG}"
        L.create_schema(PGURL, schema)
        url = L.schema_url(PGURL, schema)
        info["postgres_server_version"] = L.pg_server_version(PGURL)
    config = L.write_config(root, pg_url=url)
    storage = None
    if backend == "postgres":
        # APIRuntime does not start on PostgreSQL runtime storage (bootstrap reads
        # runtime_storage.connection; same on 0.1.11), so the PostgreSQL run uses the
        # runtime SessionStore from build_runtime_storage, the builder APIRuntime uses,
        # and passes the runs API a runtime object with that store.
        from types import SimpleNamespace

        storage = L.open_storage("postgres", url=url, work=WORK)
        rt = SimpleNamespace(sessions=storage.sessions, close=lambda: None)
        info["postgres_path"] = "build_runtime_storage(record.postgres) SessionStore; runs API with runtime=<object with .sessions>"
    else:
        rt = L.api_runtime(root, pg_url=url)
        info["sqlite_path"] = "real APIRuntime (rt.sessions); runs API with runtime=rt; installed `openminion status` CLI"
    try:
        scenario(backend, rt, root, config)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        checks.check(f"{backend}: scenario completed without an unexpected exception", False, observed=f"{type(exc).__name__}: {exc}")
    finally:
        rt.close()
        if storage is not None:
            storage.close()
        if schema:
            L.drop_schema(PGURL, schema)


run_backend("sqlite")
if PGURL:
    run_backend("postgres")
else:
    print("PostgreSQL: not run here (OPENMINION_TEST_POSTGRES_URL not set)")
    info["postgres"] = "not run on this machine"

summary = checks.summary()
write_result(OUT, {**info, "summary": summary, "checks": checks.items, "results": results})
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if checks.all_pass else 1)
