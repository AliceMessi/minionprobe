"""#352: differential check of the published packages, 0.1.10 vs 0.1.11.

Run it once with the 0.1.10 venv and once with the 0.1.11 venv, with the same
seeds and backend, and compare the outputs (probe_352_compare.py diff). Every
session stays below the 2,000-event window, so 0.1.10's window defect is not
involved: within the supported request contract the readers must give
identical results. (A run listing with a scan window smaller than the session
is deliberately not compared: there 0.1.10 reduced only the oldest window,
which is the #352 defect; those cases are checked against the event reduction
in probe_352_check.py and probe_352_contract.py.) Payloads have random values under the keys the readers read
and under an unrelated key: whitespace padding, null, booleans, numbers, empty
containers, token-like text, literal backslash escapes, NaN, +-Infinity and
NUL, in run events and in unrelated tool/attach events. Requested thread ids
hold a NUL only on SQLite; on PostgreSQL one such request per seed is made and
its outcome (an error) is recorded, as the request contract excludes it.
Adapted from MinionProbe's fuzz_372_facts.py (dev vs PR head), here on the
installed wheels.

Usage: python -I probe_352_diff.py <work_dir> <out.json> <sqlite:PATH|postgres:SCHEMA> <seed>[,<seed>...] <sessions>
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import banner, clear_override_env, quiet, write_result  # noqa: E402

import probe_352_lib as lib  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
TARGET = sys.argv[3]
SEEDS = [int(s) for s in sys.argv[4].split(",")]
COUNT = int(sys.argv[5])
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()
kind, value = lib.parse_target(TARGET)
info = banner("probe_352_diff", target=f"{kind}", seeds=SEEDS, sessions_per_seed=COUNT)

from openminion.modules.task.run import list_session_run_events, list_session_runs, resolve_thread_lifecycle  # noqa: E402

NUL = chr(0)
NAN, INF = float("nan"), float("inf")
PADDING = [chr(c) for c in range(0x3001) if chr(c).isspace()] + ["", "", "", chr(0x200B)]
EVENT_TYPES = ["run.queued", "run.running", "run.waiting_tool", "run.failed", "run.completed", "run.cancelled",
               "run.cancel_requested", "run. failed", "RUN.failed", "Run.completed", "client.attach", "client.detached",
               "response.persisted", "response.delivered", "tool.call", "tool.result"]
STATES = ["queued", "running", "failed", "completed", "cancelled", "bogus", "RUNNING"]
OTHER_VALUES = [None, True, False, 0, 1, 7, "7", "", {}, [], "None", "True"]
NONSTANDARD = [NAN, INF, -INF, NUL, f"x{NUL}y", [NAN], {"v": INF}]
RUN_IDS = ["r1", "r2", "r3", "trace: NaN", "trace: 1e999", "[NaN]", f"r{NUL}1"]
THREAD_IDS = ["t1", "t2", "trace, Infinity", f"t{NUL}"]
QUERIED_RUNS = ["r1", "r2", "r3", "7", "None", "True", "[]", "{}", "trace: NaN", "trace: 1e999", "[NaN]", f"r{NUL}1",
                "nan", "inf", "-inf", "[nan]", "{'v': inf}", f"x{NUL}y", " r1\t"]


def pad(text, rng):
    return ("".join(rng.choice(PADDING) for _ in range(rng.randint(0, 2))) + text
            + "".join(rng.choice(PADDING) for _ in range(rng.randint(0, 2))))


def value_of(rng, words):
    roll = rng.random()
    if roll < 0.5:
        return pad(rng.choice(words), rng)
    if roll < 0.75:
        return rng.choice(OTHER_VALUES)
    return rng.choice(NONSTANDARD)


def run_seed(seed: int) -> dict:
    rng = random.Random(seed)
    base = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)
    ticks = iter(range(10**9))

    def tick() -> str:  # 1 s per two appends, 8 s back every tenth
        i = next(ticks)
        return (base + timedelta(seconds=i // 2 - 8 * (i // 10))).isoformat()

    target = TARGET if kind == "sqlite" else f"postgres:{value}_{seed}"
    if kind == "sqlite":
        target = f"sqlite:{Path(value).with_name(Path(value).stem + f'-{seed}.db')}"
    else:
        lib.pg_create_schema(target.split(":", 1)[1])
    storage = lib.open_storage(target, WORK)
    out = []
    nul_request = None
    events_total = 0
    try:
        sessions = storage.sessions
        with patch(lib.EVENT_CLOCK, side_effect=tick):
            for index in range(COUNT):
                session = sessions.resolve_session(agent_id="main", channel="console", target=f"s{index}")
                for _ in range(rng.randint(5, 60)):
                    payload: dict = {}
                    for key, words in (("run_id", RUN_IDS), ("state", STATES), ("step", ["step-a", "step-b"]),
                                       ("error", ["boom", "late"]), ("conversation_id", ["conv", session.id]),
                                       ("thread_id", THREAD_IDS), ("invocation_id", ["inv-1", "inv-2"]),
                                       ("attach_id", ["a1", "a2"]), ("attach_role", ["writer", "observer", "WRITER"])):
                        if rng.random() < 0.7:
                            payload[key] = value_of(rng, words)
                    if rng.random() < 0.4:
                        payload["extra"] = rng.choice(["loss: NaN, retry", "[Infinity]", "a" + chr(92) + "u0000b",
                                                       1.5, NAN, -INF, f"bin{NUL}ary"])
                    sessions.append_event(session_id=session.id, event_type=rng.choice(EVENT_TYPES), payload=payload)
                    events_total += 1
                runs = [[r.run_id, r.state, r.current_step, r.error, r.event_count, r.started_at, r.ended_at]
                        for r in list_session_runs(sessions, session_id=session.id, limit=50)]
                events = {rid: [e.id for e in list_session_run_events(sessions, session_id=session.id, run_id=rid)]
                          for rid in QUERIED_RUNS}
                threads = {}
                requested = [t for t in THREAD_IDS if kind == "sqlite" or NUL not in t]
                for conversation_id in ("conv", session.id, ""):
                    for thread_id in requested:
                        p = resolve_thread_lifecycle(sessions, session_id=session.id, conversation_id=conversation_id, thread_id=thread_id)
                        label = f"{conversation_id == session.id}/{conversation_id == 'conv'}/{thread_id}"
                        threads[label] = [p.latest_run_id, p.latest_run_state, p.invocation_id, p.invocation_source_event_id,
                                          p.writer_attach_id, p.latest_event_id, p.delivery_state, p.thread_state,
                                          p.qualifier, p.updated_at]
                out.append({"runs": runs, "events": events, "threads": threads})
            if kind == "postgres":
                try:
                    resolve_thread_lifecycle(sessions, session_id=session.id, conversation_id="conv", thread_id=f"t{NUL}")
                    nul_request = "no error"
                except Exception as exc:  # noqa: BLE001 - recorded
                    nul_request = type(exc).__name__ + ": " + str(exc).splitlines()[0][:160]
    finally:
        storage.close()
        lib.dispose_engines()
    return {"seed": seed, "events": events_total, "sessions": out, "postgres_nul_thread_request": nul_request}


results = [run_seed(s) for s in SEEDS]
write_result(OUT, {"info": info, "results": results})
print("seeds:", SEEDS, "| events:", [r["events"] for r in results], "| nul request:", [r["postgres_nul_thread_request"] for r in results])
