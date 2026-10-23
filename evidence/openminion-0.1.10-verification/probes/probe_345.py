"""#345: run status, invocation lookup and context slices keep event insertion order
under a reversed wall clock.

The store's clock (`session_store.lifecycle.utc_now_iso`) is patched to step back,
as in the issue reproducer and the PR tests. No sleeps. Parts:
  1. event owner: list_events asc/desc, limits from both ends, prefix filter,
     stored created_at, agreement with id paging and the high water
  2. run status: the issue reproducer (queued -> running -> failed, clock steps back)
  3. invocation lookup: resolve_thread_lifecycle across two invocations of one thread
  4. context slice: build_session_slice_from_runtime_store last_event_id / tool events
  5. SDK, echo provider: a completed turn where every event append reads a clock 1 ms earlier
  6. SDK, provider 401 (MinionProbe's local scripted server): a failed turn under the same clock

Usage: python -I probe_345.py <work_dir> <out.json>
"""

from __future__ import annotations

import asyncio  # noqa: F401
import itertools
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
PROBE_REPO = Path(__file__).resolve().parents[3]  # MinionProbe repo root
sys.path.insert(0, str(PROBE_REPO))

from _common import Checks, banner, echo_config, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
quiet()
info = banner("probe_345")

from openminion.modules.storage.runtime.migrations import migrate_database  # noqa: E402
from openminion.modules.storage.runtime.session_store import SessionStore, lifecycle  # noqa: E402
from openminion.modules.storage.runtime.sqlite import connect_database  # noqa: E402
from openminion.services.runtime.run_status import (  # noqa: E402
    RUN_STATE_FAILED,
    RUN_STATE_QUEUED,
    RUN_STATE_RUNNING,
    THREAD_STATE_AWAITING,
    append_run_state_event,
    list_session_run_events,
    list_session_runs,
    resolve_thread_lifecycle,
)
from openminion.modules.context.slices import build_session_slice_from_runtime_store  # noqa: E402

c = Checks()
observed: dict[str, object] = {}
ORIGINAL_CLOCK = lifecycle.utc_now_iso


class clock:
    """Patch the event store's clock with explicit stamps or a callable."""

    def __init__(self, source):
        self.source = iter(source) if isinstance(source, list) else None
        self.fn = None if isinstance(source, list) else source

    def __enter__(self):
        lifecycle.utc_now_iso = (lambda: next(self.source)) if self.source is not None else self.fn
        return self

    def __exit__(self, *exc):
        lifecycle.utc_now_iso = ORIGINAL_CLOCK


def store(name: str):
    db = WORK / name / "state" / "openminion.db"
    migrate_database(db)
    connection = connect_database(db)
    return connection, SessionStore(connection)


# 1. event owner -------------------------------------------------------------
connection, s = store("owner")
try:
    session = s.resolve_session(agent_id="main", channel="console", target="clock-step-back")
    stamps = [
        "2026-10-21T17:16:59.564721+00:00",
        "2026-10-21T17:16:59.602657+00:00",
        "2026-10-21T17:16:58.715677+00:00",
        "2026-10-21T17:16:58.900000+00:00",
        "2026-10-21T17:16:58.800000+00:00",
    ]
    with clock(list(stamps)):
        ids = [s.append_event(session_id=session.id, event_type=f"step.{i}").id for i in range(len(stamps))]
    asc = s.list_events(session_id=session.id, limit=10)
    desc = s.list_events(session_id=session.id, limit=10, newest_first=True)
    oldest2 = s.list_events(session_id=session.id, limit=2)
    newest2 = s.list_events(session_id=session.id, limit=2, newest_first=True)
    hw = s.event_high_water(session_id=session.id)
    after = s.list_events_after_id(session_id=session.id, after_id=0, high_water_id=hw)
    before = s.list_events_before_id(session_id=session.id, before_id=hw + 1, limit=10)
    observed["owner"] = {"inserted_ids": ids, "asc_ids": [e.id for e in asc], "desc_ids": [e.id for e in desc], "stored_created_at": [e.created_at for e in asc], "high_water": hw}
    c.check("owner: ascending list_events = insertion ids", [e.id for e in asc] == ids, [e.id for e in asc], ids)
    c.check("owner: stored created_at returned as written (not reordered)", [e.created_at for e in asc] == stamps)
    c.check("owner: descending list_events = reversed insertion ids", [e.id for e in desc] == ids[::-1], [e.id for e in desc])
    c.check("owner: limit=2 oldest / newest by insertion", [e.id for e in oldest2] == ids[:2] and [e.id for e in newest2] == ids[::-1][:2], {"oldest": [e.id for e in oldest2], "newest": [e.id for e in newest2]})
    c.check("owner: newest event is the high water", newest2[0].id == hw, {"newest": newest2[0].id, "high_water": hw})
    c.check("owner: list_events_after_id / before_id pages equal list_events asc / desc", after == asc and before == desc)
    session2 = s.resolve_session(agent_id="main", channel="console", target="clock-step-back-prefix")
    entries = (
        ("run.queued", "2026-10-21T17:16:59.564721+00:00"),
        ("tool.call", "2026-10-21T17:16:59.580000+00:00"),
        ("run.running", "2026-10-21T17:16:59.602657+00:00"),
        ("run.failed", "2026-10-21T17:16:58.715677+00:00"),
        ("tool.result", "2026-10-21T17:16:58.720000+00:00"),
    )
    with clock([st for _, st in entries]):
        appended = {et: s.append_event(session_id=session2.id, event_type=et) for et, _ in entries}
    run_ids = [appended[k].id for k in ("run.queued", "run.running", "run.failed")]
    pref = s.list_events(session_id=session2.id, limit=10, event_type_prefix="run.")
    newest_run = s.list_events(session_id=session2.id, limit=1, newest_first=True, event_type_prefix="run.")
    oldest_runs = s.list_events(session_id=session2.id, limit=2, event_type_prefix="run.")
    c.check("owner: event_type_prefix filter keeps insertion order (asc, newest 1, oldest 2)",
            [e.id for e in pref] == run_ids and [e.id for e in newest_run] == run_ids[-1:] and [e.id for e in oldest_runs] == run_ids[:2],
            {"prefix": [e.id for e in pref], "newest1": [e.id for e in newest_run], "oldest2": [e.id for e in oldest_runs], "inserted_run_ids": run_ids})
finally:
    connection.close()

# 2. run status (issue reproducer) ------------------------------------------
connection, s = store("run-status")
try:
    session = s.resolve_session(agent_id="main", channel="console", target="run-status")
    queued_at = "2026-10-21T17:16:59.564721+00:00"
    failed_at = "2026-10-21T17:16:58.715677+00:00"
    with clock([queued_at, "2026-10-21T17:16:59.602657+00:00", failed_at]):
        appended = [
            append_run_state_event(s, session_id=session.id, run_id="run-1", state=st, current_step=step)
            for st, step in ((RUN_STATE_QUEUED, "turn.accepted"), (RUN_STATE_RUNNING, "agent.generate"), (RUN_STATE_FAILED, "turn.failed"))
        ]
    runs = list_session_runs(s, session_id=session.id, limit=10)
    events = list_session_run_events(s, session_id=session.id, run_id="run-1")
    latest = list_session_run_events(s, session_id=session.id, run_id="run-1", limit=1)
    summary = runs[0]
    observed["run_status"] = {
        "listed": [(e.id, e.state, e.current_step, e.created_at) for e in events],
        "run": {"state": summary.state, "current_step": summary.current_step, "started_at": summary.started_at, "ended_at": summary.ended_at, "event_count": summary.event_count},
    }
    c.check("run status: list_session_run_events lists ids in insertion order", [e.id for e in events] == [e.id for e in appended], [e.id for e in events])
    c.check("run status: states queued, running, failed", [e.state for e in events] == [RUN_STATE_QUEUED, RUN_STATE_RUNNING, RUN_STATE_FAILED], [e.state for e in events])
    c.check("run status: limit=1 returns the failed event", [e.current_step for e in latest] == ["turn.failed"], [e.current_step for e in latest])
    c.check("run status: list_session_runs reads failed / turn.failed", (summary.state, summary.current_step) == (RUN_STATE_FAILED, "turn.failed"), (summary.state, summary.current_step))
    c.check("run status: started_at / ended_at keep stored timestamps (ended_at < started_at)", summary.started_at == queued_at and summary.ended_at == failed_at and summary.event_count == 3, (summary.started_at, summary.ended_at))
finally:
    connection.close()

# 3. invocation lookup -------------------------------------------------------
connection, s = store("lifecycle")
try:
    session = s.resolve_session(agent_id="main", channel="console", target="conversation-lifecycle")
    stamps = ["2026-10-21T17:17:00.300000+00:00", "2026-10-21T17:17:00.400000+00:00", "2026-10-21T17:16:59.100000+00:00", "2026-10-21T17:16:59.200000+00:00"]
    appends = (("run-1", RUN_STATE_QUEUED, "turn.accepted"), ("run-1", RUN_STATE_FAILED, "turn.failed"), ("run-2", RUN_STATE_QUEUED, "turn.accepted"), ("run-2", RUN_STATE_RUNNING, "agent.generate"))
    with clock(list(stamps)):
        evs = [
            append_run_state_event(
                s, session_id=session.id, run_id=rid, state=st, current_step=step,
                conversation_id="conv-clock", thread_id="thread-clock",
                payload=({"invocation_id": f"invocation-{rid}"} if st == RUN_STATE_QUEUED else None),
            )
            for rid, st, step in appends
        ]
    p = resolve_thread_lifecycle(s, session_id=session.id, conversation_id="conv-clock", thread_id="thread-clock")
    observed["invocation_lookup"] = {
        "latest_run_id": p.latest_run_id, "latest_run_state": p.latest_run_state, "thread_state": p.thread_state,
        "invocation_id": p.invocation_id, "invocation_source_event_id": p.invocation_source_event_id,
        "latest_event_id": p.latest_event_id, "updated_at": p.updated_at, "inserted_ids": [e.id for e in evs],
    }
    c.check("invocation lookup: latest run is run-2 (running, awaiting)", (p.latest_run_id, p.latest_run_state, p.thread_state) == ("run-2", RUN_STATE_RUNNING, THREAD_STATE_AWAITING), (p.latest_run_id, p.latest_run_state, p.thread_state))
    c.check("invocation lookup: invocation is the second one, from its run.queued event", (p.invocation_id, p.invocation_source_event_id) == ("invocation-run-2", evs[2].id), (p.invocation_id, p.invocation_source_event_id))
    c.check("invocation lookup: latest_event_id / updated_at = last inserted event", (p.latest_event_id, p.updated_at) == (evs[3].id, evs[3].created_at), (p.latest_event_id, p.updated_at))
finally:
    connection.close()

# 4. context slice -----------------------------------------------------------
connection, s = store("slice")
try:
    session = s.resolve_session(agent_id="main", channel="console", target="clock-step-back")
    with clock(["2026-10-21T17:17:00.400000+00:00", "2026-10-21T17:16:59.100000+00:00"]):
        appended = [
            s.append_event(session_id=session.id, event_type="tool.call", payload={"tool_name": t, "summary": t})
            for t in ("web.search", "web.fetch")
        ]
    sl = build_session_slice_from_runtime_store(store=s, session_id=session.id, limits={"recent_turn_limit": 5, "tool_events_limit": 3})
    hw = s.event_high_water(session_id=session.id)
    observed["context_slice"] = {"last_event_id": sl.last_event_id, "high_water": hw, "recent_tool_events": [e.tool_name for e in sl.recent_tool_events]}
    c.check("context slice: last_event_id = last inserted event = high water", sl.last_event_id == str(appended[-1].id) == str(hw), (sl.last_event_id, hw))
    c.check("context slice: recent tool events in insertion order", [e.tool_name for e in sl.recent_tool_events] == ["web.search", "web.fetch"], [e.tool_name for e in sl.recent_tool_events])
finally:
    connection.close()


# 5/6. SDK turns -------------------------------------------------------------
def stepping_back():
    start = datetime.now(timezone.utc)
    ticks = itertools.count()
    return lambda: (start - timedelta(milliseconds=next(ticks))).isoformat()


def runtime_runs(db_path: Path):
    conn = connect_database(db_path)
    try:
        st = SessionStore(conn)
        out = []
        for sess in st.list_sessions(limit=50):
            for run in list_session_runs(st, session_id=sess.id, limit=20):
                events = list_session_run_events(st, session_id=sess.id, run_id=run.run_id)
                out.append({
                    "state": run.state, "current_step": run.current_step,
                    "events": [(e.id, e.state, e.current_step, e.created_at) for e in events],
                })
        return out
    finally:
        conn.close()


from openminion import Agent, APIRuntime  # noqa: E402

for key in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT"):
    os.environ.pop(key, None)
root = WORK / "sdk-echo"
cfg = echo_config(root)
os.environ.update({"OPENMINION_HOME": str(root), "OPENMINION_DATA_ROOT": str(root / ".openminion")})
runtime = APIRuntime.from_config_path(str(cfg), home_root=root, data_root=root / ".openminion")
try:
    with clock(stepping_back()):
        with Agent(runtime=runtime, session_id="clock-step-back") as agent:
            state = agent.run("hello").run_state
finally:
    runtime.close()
runs = runtime_runs(root / ".openminion" / "state.db")
observed["sdk_echo"] = {"run_state": state, "runs": runs}
run = runs[0] if runs else {}
ids = [e[0] for e in run.get("events", [])]
stamps = [e[3] for e in run.get("events", [])]
c.check("SDK echo turn (reversed clock): result run_state completed", state == "completed", state)
c.check("SDK echo turn (reversed clock): run reads completed", len(runs) == 1 and run["state"] == "completed", [(r["state"], r["current_step"]) for r in runs])
c.check("SDK echo turn (reversed clock): run events listed by ascending id, first queued, last completed", ids == sorted(ids) and run["events"][0][1] == "queued" and run["events"][-1][1] == "completed", [(e[0], e[1], e[2]) for e in run.get("events", [])])
c.check("SDK echo turn (reversed clock): stored created_at strictly descending (clock really stepped back)", len(stamps) >= 3 and stamps == sorted(stamps, reverse=True) and len(set(stamps)) == len(stamps), stamps)

from minionprobe.env import PROVIDERS, new_home, setup_agent  # noqa: E402
from minionprobe.fakeprovider import Fault, FakeProvider, FakeScript  # noqa: E402

fake = FakeProvider(FakeScript(faults=[Fault(status=401, when="always", message="Incorrect API key provided.")])).start()
try:
    home = new_home(WORK, "sdk-401")
    spec = PROVIDERS["local"]
    setup_agent(home, spec.agent_id, spec, base_url=fake.base_url)
    os.environ.update({"OPENMINION_HOME": str(home.root), "OPENMINION_DATA_ROOT": str(home.data_root)})
    runtime = APIRuntime.from_config_path(str(home.config_path), home_root=home.root, data_root=home.data_root)
    error = ""
    state = ""
    try:
        with clock(stepping_back()):
            with Agent(runtime=runtime, agent_id=spec.agent_id, session_id="clock-step-back-401") as agent:
                try:
                    state = agent.run("fail").run_state
                except Exception as exc:  # noqa: BLE001
                    error = type(exc).__name__
    finally:
        runtime.close()
    dbs = [p for p in home.root.rglob("*.db") if p.name in ("state.db", "openminion.db")]
    runs = []
    for db in dbs:
        try:
            runs += runtime_runs(db)
        except Exception:  # noqa: BLE001
            pass
finally:
    fake.stop()
observed["sdk_401"] = {"run_state": state, "error": error, "provider_requests": len(fake.requests), "runs": runs, "dbs": [p.name for p in dbs]}
run = runs[0] if runs else {}
steps = [(e[1], e[2]) for e in run.get("events", [])]
stamps = [e[3] for e in run.get("events", [])]
c.check("SDK provider-401 turn (reversed clock): turn failed", error != "" or state == "failed", {"run_state": state, "error": error})
c.check("SDK provider-401 turn (reversed clock): run reads failed / turn.failed", len(runs) == 1 and (run["state"], run["current_step"]) == ("failed", "turn.failed"), [(r["state"], r["current_step"]) for r in runs])
c.check("SDK provider-401 turn (reversed clock): events queued/turn.accepted, running/agent.generate, failed/turn.failed", steps == [("queued", "turn.accepted"), ("running", "agent.generate"), ("failed", "turn.failed")], steps)
c.check("SDK provider-401 turn (reversed clock): stored created_at descending", len(stamps) == 3 and stamps == sorted(stamps, reverse=True), stamps)

result = {**info, "observed": observed, "checks": c.items, "all_pass": c.all_pass}
write_result(OUT, json.loads(json.dumps(result, default=str).replace(str(WORK).replace("\\", "\\\\"), "<WORK>")))
print("ALL PASS" if c.all_pass else "SOME CHECKS FAILED")
raise SystemExit(0 if c.all_pass else 1)
