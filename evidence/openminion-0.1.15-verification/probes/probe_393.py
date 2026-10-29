"""#393 (PR #469) on the installed package: a room's first agent activation on
two independent connections, against one SQLite database or one PostgreSQL
schema.

    python -I -X utf8 probe_393.py <work_dir> <out.json> [--backend sqlite|postgres] [--rooms N]

Three runtime storages (`build_runtime_storage`, the package's runtime storage
factory) open the same database: `main` creates rooms and reads, `first` and
`second` are the two independent connections (on PostgreSQL each has its own
engine and application_name, so the server shows which backend waits for
which). Parts, in the order of the maintainer's checklist:

  A  the connections: distinct SQLite connections on one file, or distinct
     engines on one PostgreSQL schema.
  B  held joins, at the seam of upstream's tests (`held_activation_read`) and
     of the report's probe: the first session read after the join writes its
     participant row, the read that decides the activation.
       B1 overlapping first joins; B2 a join plus explicit selection on the
       other connection; B2b a selection alone (a legacy session whose owner
       record activates nobody); B3 the joining agent leaves.
  C  the first committed activation stays through joins, re-joins and leaves of
     other agents, until an explicit selection or a leave of the active agent.
  D  existing active selections (written by connections closed before the
     joins; set at room creation), blank/empty initial activation, rejoin,
     legacy owner behavior, join ordering (a tied clock).
  F  transaction paths: the participant row and the activation commit together;
     an exception at the activation read, after the activation write, or in a
     held join while the other connection waits; a real lock error on the
     waiting connection (SQLite busy timeout, PostgreSQL lock_timeout).
  G  unpaused stress, N rooms each: two first joins; a first join against a
     join plus selection; a first join against a leave of the joining agent.
  E  reopen: every storage closed, then a new process opens a new runtime
     storage on the same database and reads each room; the rows are also read
     independently of openminion (sqlite3 / SQLAlchemy on the server).
  N  a note only, not a check: #495 (selection versus leave, PR #520, not in
     0.1.15) on a separate database.

In-process by necessity: the holds and the injected exceptions wrap the
participant owner's session read or the backend's execute_count; the wrapped
call itself is unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, quiet, write_result  # noqa: E402
from probe_393_lib import (  # noqa: E402
    HOLD_SECONDS,
    HOLD_TIMEOUT,
    PARTICIPANTS,
    Op,
    PgEnv,
    SqliteEnv,
    WriteLog,
    agents,
    departed_or_unknown_active,
    describe,
    held,
    instrument,
    join,
    join_and_read_active,
    labelled,
    leave,
    mask,
    new_room,
    pg_wait_ok,
    select,
    view,
    watch_while_held,
)

CASES: dict[str, Any] = {}
EXPECT: dict[str, dict[str, Any]] = {}
TIED = "2026-10-29T09:00:00+00:00"


class Ctx:
    def __init__(self, checks: Checks, env: Any, label: str) -> None:
        self.checks, self.env, self.L = checks, env, label
        self.log = WriteLog()
        self.st: dict[str, Any] = {}
        for role in ("main", "first", "second"):
            self.st[role] = instrument(env.open(role), role, self.log)
        self.main, self.first, self.second = self.st["main"], self.st["first"], self.st["second"]

    def check(self, name: str, ok: bool, observed: Any = None) -> bool:
        return self.checks.check(f"{self.L} {name}", ok, observed=observed)

    def expect(self, room: str, case: str, stored: Any, agents_: list[str], active: Any = "same") -> None:
        EXPECT[room] = {"case": case, "stored": stored, "agents": agents_,
                        "active": stored if active == "same" else active}

    def close(self) -> None:
        for storage in self.st.values():
            try:
                storage.close()
            except Exception as exc:  # noqa: BLE001
                print("close failed:", describe(exc))


def part(checks: Checks, label: str, name: str, fn: Any, *args: Any) -> None:
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        checks.check(f"{label} {name}: completes without an exception", False, observed=describe(exc))


# ======================================================================= A
def part_a(c: Ctx) -> None:
    info = c.env.describe_connections(c.st)
    CASES[f"{c.L} A connections"] = info
    if c.env.pg:
        CASES[f"{c.L} A server"] = c.env.server()
        ok = (len({v["engine_id"] for v in info.values()}) == 3 and len({v["app"] for v in info.values()}) == 3
              and {v["schema"] for v in info.values()} == {c.env.schema})
        c.check("A: main, first and second are independent PostgreSQL connections (own engine and application_name) "
                "on one schema", ok, observed=info)
    else:
        ok = (len({v["connection_id"] for v in info.values()}) == 3 and len({v["file"] for v in info.values()}) == 1
              and all(v["journal_mode"] == "wal" for v in info.values()))
        c.check("A: main, first and second are independent sqlite3 connections (WAL) on one database file", ok, observed=info)


# ======================================================================= B
def part_b1(c: Ctx) -> None:
    env, log = c.env, c.log
    room = new_room(c.main, "b1-two-first-joins")
    mark = log.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, held(c.first) as hold:
        a = Op(pool, "B1 join alpha (first, held)", join_and_read_active, c.first, room, "alpha")
        assert hold.read.wait(timeout=HOLD_TIMEOUT)
        during = env.raw_room(room)
        b = Op(pool, "B1 join beta (second)", join_and_read_active, c.second, room, "beta")
        watch = watch_while_held(b, env, "second", "first")
        t_release = time.monotonic()
        hold.release.set()
        ra, rb = a.outcome(), b.outcome()
    final, raw = view(c.main, room), env.raw_room(room)
    writes = log.brief(log.since(mark, room))
    CASES[f"{c.L} B1"] = {"room": room, "held_read_saw": hold.seen, "held_in_transaction": hold.in_transaction,
                          "independent_read_while_held": during, "watch": watch, "alpha": ra, "beta": rb,
                          "beta_seconds": b.seconds(), "beta_ended_after_release": (b.t_end or 0) >= t_release,
                          "final": final, "raw": raw, "active_agent_writes": writes}
    c.check("B1: the held join's activation read found no active agent", hold.seen is None, observed=hold.seen)
    c.check("B1: at that read the held join is inside its participant transaction (session-row lock held)",
            hold.in_transaction is True, observed=hold.in_transaction)
    c.check("B1: while the join is held, another connection sees neither its participant row nor an active agent",
            "alpha" not in during["agents"] and during["stored"] is None, observed=during)
    c.check("B1: beta's join on the other connection waits for the held join (not finished during the "
            f"{HOLD_SECONDS} s hold; ends after the release" + ("; the server shows its backend blocked on a lock held by "
                                                              "the held join's backend)" if env.pg else ")"),
            not watch["finished_while_held"] and (b.t_end or 0) >= t_release and pg_wait_ok(env, watch),
            observed={"finished_while_held": watch["finished_while_held"], "beta_seconds": b.seconds(),
                      "pg_lock_wait": (watch["pg_lock_wait"] or {}).get("waiter")})
    c.check("B1: no stale overwrite: each caller saw alpha, alpha is stored, agents listed [alpha, beta]",
            ra == "ok: 'alpha'" and rb == "ok: 'alpha'" and final == {"stored": "alpha", "active": "alpha",
                                                                      "agents": ["alpha", "beta"]},
            observed={"alpha caller": ra, "beta caller": rb, "final": final})
    c.check("B1: exactly one active-agent write, by alpha's join", writes == [["first", "B1 join alpha (first, held)", "alpha"]],
            observed=writes)
    c.expect(room, "B1", "alpha", ["alpha", "beta"])


def part_b2(c: Ctx) -> None:
    env, log = c.env, c.log
    room = new_room(c.main, "b2-selection")

    def join_and_select_beta() -> Any:
        join(c.second, room, "beta")
        return select(c.second, room, "beta")

    mark = log.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, held(c.first) as hold:
        a = Op(pool, "B2 join alpha (first, held)", join, c.first, room, "alpha")
        assert hold.read.wait(timeout=HOLD_TIMEOUT)
        s = Op(pool, "B2 join beta + select beta (second)", join_and_select_beta)
        watch = watch_while_held(s, env, "second", "first")
        t_release = time.monotonic()
        hold.release.set()
        ra, rs = a.outcome(), s.outcome()
    final = view(c.main, room)
    writes = log.brief(log.since(mark, room))
    CASES[f"{c.L} B2"] = {"room": room, "held_read_saw": hold.seen, "watch": watch, "alpha": ra, "selection": rs,
                          "final": final, "raw": env.raw_room(room), "active_agent_writes": writes}
    c.check("B2: the join plus selection on the other connection waits for the held join"
            + (" (server: blocked on the held join's lock)" if env.pg else ""),
            not watch["finished_while_held"] and (s.t_end or 0) >= t_release and pg_wait_ok(env, watch),
            observed={"finished_while_held": watch["finished_while_held"], "seconds": s.seconds(),
                      "pg_lock_wait": (watch["pg_lock_wait"] or {}).get("waiter")})
    c.check("B2: the selection then applies: it returns beta, beta is stored and active, agents [alpha, beta]",
            ra == "ok: 'alpha'" and rs == "ok: 'beta'" and final == {"stored": "beta", "active": "beta",
                                                                     "agents": ["alpha", "beta"]},
            observed={"alpha join": ra, "selection": rs, "final": final})
    c.check("B2: the writes are alpha's activation, then the selection's beta (the join wrote nothing over the selection)",
            writes == [["first", "B2 join alpha (first, held)", "alpha"], ["second", "B2 join beta + select beta (second)", "beta"]],
            observed=writes)
    c.expect(room, "B2", "beta", ["alpha", "beta"])


def part_b2b(c: Ctx) -> None:
    env, log = c.env, c.log
    legacy = c.main.sessions.resolve_session(agent_id="legacy-agent", channel="console", target="b2b-selection-alone")
    room = legacy.id
    before = view(c.main, room)  # the read writes the legacy owner record, which activates nobody
    mark = log.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, held(c.first) as hold:
        a = Op(pool, "B2b join guest-agent (first, held)", join, c.first, room, "guest-agent")
        assert hold.read.wait(timeout=HOLD_TIMEOUT)
        s = Op(pool, "B2b select legacy-agent (second)", select, c.second, room, "legacy-agent")
        watch = watch_while_held(s, env, "second", "first")
        t_release = time.monotonic()
        hold.release.set()
        ra, rs = a.outcome(), s.outcome()
    final = view(c.main, room)
    writes = log.brief(log.since(mark, room))
    CASES[f"{c.L} B2b"] = {"room": room, "before": before, "held_read_saw": hold.seen, "watch": watch, "guest": ra,
                           "selection": rs, "final": final, "raw": env.raw_room(room), "active_agent_writes": writes}
    c.check("B2b: setup: a legacy session whose owner record is listed, with no stored active agent",
            before == {"stored": None, "active": "legacy-agent", "agents": ["legacy-agent"]}, observed=before)
    c.check("B2b: a selection alone on the other connection waits for the held join"
            + (" (server: blocked on the held join's lock)" if env.pg else ""),
            not watch["finished_while_held"] and (s.t_end or 0) >= t_release and pg_wait_ok(env, watch),
            observed={"finished_while_held": watch["finished_while_held"], "seconds": s.seconds(),
                      "pg_lock_wait": (watch["pg_lock_wait"] or {}).get("waiter")})
    c.check("B2b: the selection then applies over the join's activation: legacy-agent stored and active",
            ra == "ok: 'guest-agent'" and rs == "ok: 'legacy-agent'"
            and final == {"stored": "legacy-agent", "active": "legacy-agent", "agents": ["legacy-agent", "guest-agent"]},
            observed={"guest join": ra, "selection": rs, "final": final})
    c.check("B2b: the writes are the guest join's activation, then the selection",
            writes == [["first", "B2b join guest-agent (first, held)", "guest-agent"],
                       ["second", "B2b select legacy-agent (second)", "legacy-agent"]], observed=writes)
    c.expect(room, "B2b", "legacy-agent", ["legacy-agent", "guest-agent"])


def part_b3(c: Ctx) -> None:
    env, log = c.env, c.log
    room = new_room(c.main, "b3-leave")
    left = threading.Event()
    owner = c.first.sessions._participants
    get_participant = owner.get_participant

    def get_participant_after_the_leave(*args: Any, **kwargs: Any) -> Any:
        # As upstream's test: the held join reads its participant back only after the leave returned.
        assert left.wait(timeout=HOLD_TIMEOUT)
        return get_participant(*args, **kwargs)

    mark = log.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, held(c.first) as hold, \
            patch.object(owner, "get_participant", get_participant_after_the_leave):
        a = Op(pool, "B3 join alpha (first, held)", join, c.first, room, "alpha")
        assert hold.read.wait(timeout=HOLD_TIMEOUT)
        lv = Op(pool, "B3 leave alpha (second)", leave, c.second, room, "alpha")
        watch = watch_while_held(lv, env, "second", "first")
        t_release = time.monotonic()
        hold.release.set()
        rl = lv.outcome()
        left.set()
        ra = a.outcome()
    final = view(c.main, room)
    try:
        select(c.main, room, "alpha")
        reselect = "accepted"
    except ValueError as exc:
        reselect = describe(exc)
    writes = log.brief(log.since(mark, room))
    CASES[f"{c.L} B3"] = {"room": room, "held_read_saw": hold.seen, "watch": watch, "leave": rl, "alpha": ra,
                          "final": final, "raw": env.raw_room(room), "select alpha afterwards": reselect,
                          "active_agent_writes": writes}
    c.check("B3: the leave on the other connection waits for the held join"
            + (" (server: blocked on the held join's lock)" if env.pg else ""),
            not watch["finished_while_held"] and (lv.t_end or 0) >= t_release and pg_wait_ok(env, watch),
            observed={"finished_while_held": watch["finished_while_held"], "seconds": lv.seconds(),
                      "pg_lock_wait": (watch["pg_lock_wait"] or {}).get("waiter")})
    c.check("B3: the leave then applies: it returns True, no agent listed, no active agent stored (no departed active agent)",
            rl == "ok: True" and final == {"stored": None, "active": None, "agents": []}, observed={"leave": rl, "final": final})
    c.check("B3: the held join reads its participant back after the leave and raises, as before #393",
            "RuntimeError: Failed to upsert participant agent:alpha" in ra, observed=ra)
    c.check("B3: selecting the departed alpha is refused", "not an active participant" in reselect, observed=reselect)
    c.expect(room, "B3", None, [])


# ======================================================================= C
def part_c(c: Ctx) -> None:
    log = c.log
    room = new_room(c.main, "c-first-activation-remains")
    steps = [
        ("first: join alpha (first activation)", lambda: join(c.first, room, "alpha"), "alpha"),
        ("second: join beta", lambda: join(c.second, room, "beta"), "alpha"),
        ("first: join gamma", lambda: join(c.first, room, "gamma"), "alpha"),
        ("second: join alpha again (already a participant)", lambda: join(c.second, room, "alpha"), "alpha"),
        ("first: gamma leaves (not the active agent)", lambda: leave(c.first, room, "gamma"), "alpha"),
        ("second: select beta (intentional selection)", lambda: select(c.second, room, "beta"), "beta"),
        ("first: join delta", lambda: join(c.first, room, "delta"), "beta"),
        ("first: beta leaves (the active agent: intentional leave)", lambda: leave(c.first, room, "beta"), "alpha"),
        ("second: alpha leaves (the active agent)", lambda: leave(c.second, room, "alpha"), "delta"),
        ("first: delta leaves (the last agent)", lambda: leave(c.first, room, "delta"), None),
        ("second: gamma joins again (rejoin into a room with no agent)", lambda: join(c.second, room, "gamma"), "gamma"),
    ]
    timeline, ok = [], True
    mark = log.mark()
    for name, action, expected in steps:
        with labelled(f"C {name}"):
            result = action()
        seen = {"stored": view(c.main, room)["stored"], "first": c.first.sessions.get_active_agent(room),
                "second": c.second.sessions.get_active_agent(room)}
        good = seen == {"stored": expected, "first": expected, "second": expected}
        ok = ok and good
        timeline.append([name, repr(result), seen, expected, good])
    writes = log.brief(log.since(mark, room))
    join_writes = [w for w in writes if "join" in w[1]]
    CASES[f"{c.L} C"] = {"room": room, "timeline": timeline, "active_agent_writes": writes, "final": view(c.main, room)}
    c.check("C: the first committed activation (alpha) stays through joins, a re-join and a leave of another agent; "
            "it changes only on the explicit selection and on leaves of the active agent; both connections read the "
            "stored agent after every step", ok, observed=[[t[0], t[2]["stored"], t[4]] for t in timeline])
    c.check("C: only the two joins into a room with no active agent wrote it (alpha first, gamma last)",
            [w[2] for w in join_writes] == ["alpha", "gamma"], observed=join_writes)
    c.expect(room, "C", "gamma", ["gamma"])


# ======================================================================= D
def part_d_existing(c: Ctx) -> None:
    env, log = c.env, c.log
    # (i) a selection written by connections that are closed before the joins
    seed = env.open("seed")
    room = new_room(seed, "d-existing-selection")
    join(seed, room, "alpha")
    join(seed, room, "beta")
    select(seed, room, "beta")
    seed.close()
    mark = log.mark()
    join(c.first, room, "gamma")
    barrier = threading.Barrier(2)

    def at_barrier(storage: Any, agent: str) -> Any:
        barrier.wait(HOLD_TIMEOUT)
        return join_and_read_active(storage, room, agent)

    with ThreadPoolExecutor(max_workers=2) as pool:
        d1 = Op(pool, "D join delta (first)", at_barrier, c.first, "delta")
        d2 = Op(pool, "D join epsilon (second)", at_barrier, c.second, "epsilon")
        concurrent = [d1.outcome(), d2.outcome()]
    with ThreadPoolExecutor(max_workers=2) as pool, held(c.first) as hold:
        z = Op(pool, "D join zeta (first, held)", join_and_read_active, c.first, room, "zeta")
        assert hold.read.wait(timeout=HOLD_TIMEOUT)
        e = Op(pool, "D join eta (second)", join_and_read_active, c.second, room, "eta")
        watch = watch_while_held(e, env, "second", "first")
        hold.release.set()
        held_results = [z.outcome(), e.outcome()]
    join(c.second, room, "alpha")  # the earlier first activation joins again
    final = view(c.main, room)
    writes = log.brief(log.since(mark, room))
    CASES[f"{c.L} D existing selection"] = {"room": room, "concurrent": concurrent, "held_read_saw": hold.seen,
                                             "held": held_results, "watch": watch, "final": final, "writes": writes}
    c.check("D existing selection: a selection stored by connections closed before the joins (beta) stays through a join, "
            "two concurrent joins, a held join with another join waiting, and a re-join of the first agent; "
            "every caller saw beta",
            final["stored"] == "beta" and final["active"] == "beta" and concurrent == ["ok: 'beta'", "ok: 'beta'"]
            and held_results == ["ok: 'beta'", "ok: 'beta'"] and hold.seen == "beta",
            observed={"final": final, "concurrent": concurrent, "held": held_results, "held read saw": hold.seen})
    c.check("D existing selection: none of those joins wrote the active agent", writes == [], observed=writes)
    c.check("D existing selection: every agent that joined is listed once (delta and epsilon in either order)",
            sorted(final["agents"]) == sorted(["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta"])
            and final["agents"][:3] == ["alpha", "beta", "gamma"], observed=final["agents"])
    # The order of the two concurrent joins is not fixed; part E compares the reopened room with this listing.
    c.expect(room, "D existing selection", "beta", final["agents"])

    # (ii) an active agent set at room creation (create_room's active_agent_id)
    room2 = c.main.sessions.create_room(channel="cli", target="d-initial-activation", active_agent_id="beta").id
    c.main.sessions.add_participant(session_id=room2, participant_type="human", participant_id="owner", role="owner")
    raw_before = env.raw_room(room2)
    mark = log.mark()
    join(c.first, room2, "alpha")
    after_alpha = view(c.main, room2)
    join(c.second, room2, "beta")
    final2 = view(c.main, room2)
    writes2 = log.brief(log.since(mark, room2))
    CASES[f"{c.L} D initial activation"] = {"room": room2, "raw_before": raw_before, "after_alpha": after_alpha,
                                             "final": final2, "writes": writes2}
    c.check("D initial activation: a room created with active_agent_id beta keeps beta when alpha, then beta, join "
            "(no join wrote it)",
            raw_before["stored"] == "beta" and after_alpha["stored"] == "beta" and final2["stored"] == "beta"
            and writes2 == [], observed={"raw before": raw_before["stored"], "after alpha": after_alpha, "final": final2,
                                         "writes": writes2})
    c.expect(room2, "D initial activation", "beta", ["alpha", "beta"])


def part_d_blank(c: Ctx) -> None:
    env, log = c.env, c.log
    results = []
    # create_room's own initial value (None, "" or spaces), then blank values written into the row directly,
    # as upstream's test does, for rows another writer left blank.
    for name, value, direct in (("none", None, False), ("empty", "", False), ("spaces", "   ", False),
                                ("empty-row", "", True), ("spaces-row", "   ", True)):
        room = c.main.sessions.create_room(channel="cli", target=f"d-blank-{name}",
                                           active_agent_id=None if direct else value).id
        c.main.sessions.add_participant(session_id=room, participant_type="human", participant_id="owner", role="owner")
        if direct:
            env.raw_set_active(room, value)
        raw = env.raw_room(room)["stored"]
        before = view(c.main, room)["active"]
        mark = log.mark()
        join(c.second, room, "alpha")
        final = view(c.main, room)
        writes = log.brief(log.since(mark, room))
        results.append({"case": name, "initial": value, "written directly": direct, "raw stored before": raw,
                        "get_active_agent before": before, "final": final, "writes": writes})
        c.expect(room, f"D blank {name}", "alpha", ["alpha"])
    # a held join on a row whose active agent is spaces only, with a second join waiting
    room = new_room(c.main, "d-blank-held")
    env.raw_set_active(room, "   ")
    raw_held_before = env.raw_room(room)["stored"]
    mark = log.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, held(c.first) as hold:
        a = Op(pool, "D blank join alpha (first, held)", join_and_read_active, c.first, room, "alpha")
        assert hold.read.wait(timeout=HOLD_TIMEOUT)
        b = Op(pool, "D blank join beta (second)", join_and_read_active, c.second, room, "beta")
        watch = watch_while_held(b, env, "second", "first")
        hold.release.set()
        held_results = [a.outcome(), b.outcome()]
    final_h = view(c.main, room)
    writes_h = log.brief(log.since(mark, room))
    c.expect(room, "D blank held", "alpha", ["alpha", "beta"])
    CASES[f"{c.L} D blank"] = {"rooms": results, "held": {"room": room, "raw stored before": raw_held_before,
                                                         "held_read_saw": hold.seen, "results": held_results,
                                                         "watch": watch, "final": final_h, "writes": writes_h}}
    c.check("D blank: rooms created with no, empty or spaces-only initial activation (create_room stores each as NULL), "
            "and rows whose active_agent_id is '' or '   ' (written directly): the first joining agent (alpha) is stored "
            "and active, one write each",
            [r["raw stored before"] for r in results] == [None, None, None, "", "   "]
            and all(r["final"] == {"stored": "alpha", "active": "alpha", "agents": ["alpha"]}
                    and [[w[0], w[2]] for w in r["writes"]] == [["second", "alpha"]] for r in results),
            observed=[[r["raw stored before"], r["final"], r["writes"]] for r in results])
    c.check("D blank: on a row whose active_agent_id is '   ', a held first join (its session read gives no active "
            "agent) activates its agent, and the second join waiting for it keeps it",
            raw_held_before == "   " and hold.seen is None and held_results == ["ok: 'alpha'", "ok: 'alpha'"]
            and final_h["stored"] == "alpha" and [w[2] for w in writes_h] == ["alpha"] and not watch["finished_while_held"],
            observed={"held read saw": hold.seen, "results": held_results, "final": final_h, "writes": writes_h,
                      "waiter finished while held": watch["finished_while_held"]})


def part_d_rejoin(c: Ctx) -> None:
    env, log = c.env, c.log
    room = new_room(c.main, "d-rejoin")
    mark = log.mark()
    join(c.first, room, "alpha")
    join(c.second, room, "beta")
    seq = {r[1]: r[3] for r in env.raw_room(room)["participants"]}
    leave(c.second, room, "alpha")
    s1 = view(c.main, room)
    join(c.first, room, "alpha")  # rejoin while beta is active
    s2 = view(c.main, room)
    raw2 = env.raw_room(room)
    seq2 = {r[1]: r[3] for r in raw2["participants"]}
    join(c.second, room, "alpha")  # an active participant joins again: keeps its place
    raw3 = env.raw_room(room)
    leave(c.first, room, "beta")
    s3 = view(c.main, room)
    leave(c.second, room, "alpha")
    s4 = view(c.main, room)
    join(c.first, room, "alpha")  # rejoin into a room with no agent
    s5 = view(c.main, room)
    writes = log.brief(log.since(mark, room))
    CASES[f"{c.L} D rejoin"] = {"room": room, "seq": seq, "after alpha leaves": s1, "after alpha rejoins": s2,
                                "raw after rejoin": raw2, "raw after joining again": raw3, "after beta leaves": s3,
                                "after alpha leaves again": s4, "after alpha rejoins an empty room": s5, "writes": writes}
    c.check("D rejoin: alpha leaves (beta becomes active), rejoins: beta stays active and alpha is listed after beta "
            "with a new join_seq; joining again while present keeps its place",
            s1["stored"] == "beta" and s2 == {"stored": "beta", "active": "beta", "agents": ["beta", "alpha"]}
            and seq2["alpha"] > seq["beta"] > seq["alpha"] and raw3["participants"] == raw2["participants"],
            observed={"after leave": s1, "after rejoin": s2, "join_seq before": seq, "join_seq after": seq2})
    c.check("D rejoin: with no agent left, the rejoining alpha is activated",
            s3["stored"] == "alpha" and s4 == {"stored": None, "active": None, "agents": []}
            and s5 == {"stored": "alpha", "active": "alpha", "agents": ["alpha"]},
            observed={"after beta leaves": s3, "after alpha leaves": s4, "after rejoin": s5})
    c.check("D rejoin: the active agent was written only by the two joins into a room without one (alpha) and by the "
            "leaves of the active agent (beta, alpha, None)",
            [[w[0], w[2]] for w in writes] == [["first", "alpha"], ["second", "beta"], ["first", "alpha"],
                                               ["second", None], ["first", "alpha"]],
            observed=writes)
    c.expect(room, "D rejoin", "alpha", ["alpha"])


def part_d_legacy(c: Ctx) -> None:
    log = c.log
    out = {}
    # read: the owner record is written when the participants are first read, on the other connection
    s = c.main.sessions.resolve_session(agent_id="legacy-agent", channel="console", target="legacy-read")
    mark = log.mark()
    listed = [[p.participant_id, p.role] for p in c.second.sessions.list_participants(s.id)]
    out["read"] = {"listed": listed, "view": view(c.main, s.id), "writes": log.brief(log.since(mark, s.id))}
    c.expect(s.id, "D legacy read", None, ["legacy-agent"], active="legacy-agent")
    # invite: another agent joins first; the owner record is written by that join, without activation
    s = c.main.sessions.resolve_session(agent_id="legacy-agent", channel="console", target="legacy-invite")
    mark = log.mark()
    with labelled("D legacy join guest-agent (first)"):
        join(c.first, s.id, "guest-agent")
    out["invite"] = {"listed": [[p.participant_id, p.role] for p in c.main.sessions.list_participants(s.id)],
                     "view": view(c.main, s.id), "writes": log.brief(log.since(mark, s.id))}
    c.expect(s.id, "D legacy invite", "guest-agent", ["legacy-agent", "guest-agent"])
    # own: the session's own agent joins
    s = c.main.sessions.resolve_session(agent_id="legacy-agent", channel="console", target="legacy-own")
    mark = log.mark()
    with labelled("D legacy join legacy-agent (second)"):
        join(c.second, s.id, "legacy-agent")
    out["own"] = {"view": view(c.main, s.id), "writes": log.brief(log.since(mark, s.id))}
    c.expect(s.id, "D legacy own", "legacy-agent", ["legacy-agent"])
    CASES[f"{c.L} D legacy"] = out
    c.check("D legacy: reading a legacy session's participants writes its owner record and activates nobody "
            "(stored None, get_active_agent answers the session's agent, no write)",
            out["read"]["listed"] == [["legacy-agent", "owner"]] and out["read"]["writes"] == []
            and out["read"]["view"] == {"stored": None, "active": "legacy-agent", "agents": ["legacy-agent"]},
            observed=out["read"])
    c.check("D legacy: another agent joining writes the owner record without activation, then activates the joining "
            "agent (one write)", out["invite"]["listed"] == [["legacy-agent", "owner"], ["guest-agent", "participant"]]
            and out["invite"]["view"]["stored"] == "guest-agent"
            and out["invite"]["writes"] == [["first", "D legacy join guest-agent (first)", "guest-agent"]],
            observed=out["invite"])
    c.check("D legacy: the session's own agent joining is activated",
            out["own"]["view"]["stored"] == "legacy-agent"
            and out["own"]["writes"] == [["second", "D legacy join legacy-agent (second)", "legacy-agent"]],
            observed=out["own"])


def part_d_order(c: Ctx) -> None:
    env = c.env
    with patch(f"{PARTICIPANTS}.utc_now_iso", return_value=TIED):
        room = new_room(c.main, "d-order")
        order = [("a1", c.first), ("a2", c.second), ("a3", c.first), ("a4", c.second)]
        for agent, storage in order:
            join(storage, room, agent)
        lists = {role: [[p.participant_type, p.participant_id] for p in c.st[role].sessions.list_participants(room)]
                 for role in ("main", "first", "second")}
        raw = env.raw_room(room)
        leave(c.second, room, "a2")
        join(c.first, room, "a2")
        after = env.raw_room(room)
    final = view(c.main, room)
    CASES[f"{c.L} D order"] = {"room": room, "lists": lists, "raw": raw, "after a2 rejoins": after, "final": final}
    expected = [["human", "owner"], ["agent", "a1"], ["agent", "a2"], ["agent", "a3"], ["agent", "a4"]]
    c.check("D order: with one tied joined_at, joins alternating between the two connections list in join order on all "
            "three connections (join_seq 1..5); the first agent is the active one",
            all(v == expected for v in lists.values()) and [r[3] for r in raw["participants"]] == [1, 2, 3, 4, 5]
            and final["stored"] == "a1", observed={"lists": lists["main"], "join_seq": [r[3] for r in raw["participants"]],
                                                  "stored": final["stored"]})
    c.check("D order: a2 leaves and rejoins on the tied clock: listed last with join_seq 6, a1 still active",
            [r[1] for r in after["participants"]] == ["owner", "a1", "a3", "a4", "a2"]
            and after["participants"][-1][3] == 6 and final["stored"] == "a1",
            observed={"order": [r[1] for r in after["participants"]], "join_seq": [r[3] for r in after["participants"]]})
    c.expect(room, "D order", "a1", ["a1", "a3", "a4", "a2"])


# ======================================================================= F
def part_f(c: Ctx) -> None:
    env, log = c.env, c.log
    # F1 normal path: the participant row and the activation commit together
    room = new_room(c.main, "f1-commit")
    with ThreadPoolExecutor(max_workers=1) as pool, held(c.first, seam="after-activation-write") as hold:
        a = Op(pool, "F1 join alpha (first, held after its activation write)", join, c.first, room, "alpha")
        assert hold.read.wait(timeout=HOLD_TIMEOUT)
        during = env.raw_room(room)
        hold.release.set()
        ra = a.outcome()
    after = env.raw_room(room)
    CASES[f"{c.L} F1"] = {"room": room, "in_transaction": hold.in_transaction, "during": during, "after": after, "join": ra}
    c.check("F1 normal path: held right after its activation write, the join is still in its transaction and an "
            "independent reader sees neither its row nor the activation; after it returns, both",
            hold.in_transaction is True and during["agents"] == [] and during["stored"] is None
            and ra == "ok: 'alpha'" and after["agents"] == ["alpha"] and after["stored"] == "alpha",
            observed={"in_transaction": hold.in_transaction, "during": [during["agents"], during["stored"]],
                      "after": [after["agents"], after["stored"]]})
    c.expect(room, "F1", "alpha", ["alpha"])

    # F2 an exception at the activation read
    room = new_room(c.main, "f2-fail-at-read")
    mark = log.mark()
    with held(c.first, fail=RuntimeError("probe: injected failure at the activation read")) as hold:
        hold.release.set()
        with labelled("F2 join alpha (first, fails at its activation read)"):
            try:
                join(c.first, room, "alpha")
                r2 = "ok"
            except Exception as exc:  # noqa: BLE001
                r2 = describe(exc)
    raw = env.raw_room(room)
    in_tx = bool(c.first.record_store.in_transaction)
    locks = env.locks_held("first")
    t0 = time.monotonic()
    with labelled("F2 join beta (second)"):
        join(c.second, room, "beta")
    beta_seconds = round(time.monotonic() - t0, 3)
    with labelled("F2 join alpha again (first)"):
        join(c.first, room, "alpha")
    final = view(c.main, room)
    writes = log.brief(log.since(mark, room))
    CASES[f"{c.L} F2"] = {"room": room, "join": r2, "raw after failure": raw, "first in_transaction": in_tx,
                          "pg locks held by first": locks, "beta join seconds": beta_seconds, "final": final, "writes": writes}
    c.check("F2 exception at the activation read: the join raises it and nothing of it is stored (no alpha row, no "
            "active agent)", "injected failure at the activation read" in r2 and raw["agents"] == [] and raw["stored"] is None,
            observed={"join": r2, "raw": [raw["agents"], raw["stored"], raw["left"]]})
    c.check("F2: the transaction is closed and its lock released: the connection is not in a transaction"
            + (", holds no lock on the server" if env.pg else "") + ", and a join on the other connection completes at once",
            not in_tx and locks == 0 and beta_seconds < 1.0, observed={"in_transaction": in_tx, "locks": locks,
                                                                      "beta join seconds": beta_seconds})
    c.check("F2: the same connection then joins normally (beta, joined first, stays active)",
            final == {"stored": "beta", "active": "beta", "agents": ["beta", "alpha"]}, observed=final)
    c.expect(room, "F2", "beta", ["beta", "alpha"])

    # F3 an exception after the activation write
    room = new_room(c.main, "f3-fail-after-write")
    mark = log.mark()
    with held(c.first, seam="after-activation-write",
              fail=RuntimeError("probe: injected failure after the activation write")) as hold:
        hold.release.set()
        with labelled("F3 join alpha (first, fails after its activation write)"):
            try:
                join(c.first, room, "alpha")
                r3 = "ok"
            except Exception as exc:  # noqa: BLE001
                r3 = describe(exc)
    written = [dict((k, v) for k, v in e.items() if k != "t") for e in log.since(mark, room)]
    raw = env.raw_room(room)
    in_tx = bool(c.first.record_store.in_transaction)
    with labelled("F3 join beta (second)"):
        join(c.second, room, "beta")
    final = view(c.main, room)
    CASES[f"{c.L} F3"] = {"room": room, "join": r3, "activation write ran": written, "raw after failure": raw,
                          "first in_transaction": in_tx, "final": final}
    c.check("F3 exception after the activation write: the write ran inside the join's transaction, the join raises, "
            "and the row and the activation are both rolled back",
            "injected failure after the activation write" in r3 and len(written) == 1 and written[0]["in_transaction"] is True
            and raw["agents"] == [] and raw["stored"] is None and not in_tx,
            observed={"join": r3, "write": written, "raw": [raw["agents"], raw["stored"]], "in_transaction": in_tx})
    c.check("F3: the next join (beta, on the other connection) is the room's first activation",
            final == {"stored": "beta", "active": "beta", "agents": ["beta"]}, observed=final)
    c.expect(room, "F3", "beta", ["beta"])

    # F4 an exception in a held join while the other connection waits
    room = new_room(c.main, "f4-fail-while-waited")
    mark = log.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, \
            held(c.first, fail=RuntimeError("probe: injected failure in the held join")) as hold:
        a = Op(pool, "F4 join alpha (first, held, then fails)", join, c.first, room, "alpha")
        assert hold.read.wait(timeout=HOLD_TIMEOUT)
        b = Op(pool, "F4 join beta (second)", join_and_read_active, c.second, room, "beta")
        watch = watch_while_held(b, env, "second", "first")
        hold.release.set()
        ra, rb = a.outcome(), b.outcome()
    final = view(c.main, room)
    writes = log.brief(log.since(mark, room))
    CASES[f"{c.L} F4"] = {"room": room, "watch": watch, "alpha": ra, "beta": rb, "final": final, "writes": writes}
    c.check("F4 exception in a held join while beta's join waits"
            + (" (server: blocked on the held join's lock)" if env.pg else "")
            + ": alpha's join raises and leaves nothing; beta's join then proceeds and is the first activation",
            not watch["finished_while_held"] and pg_wait_ok(env, watch) and "injected failure in the held join" in ra
            and rb == "ok: 'beta'" and final == {"stored": "beta", "active": "beta", "agents": ["beta"]}
            and writes == [["second", "F4 join beta (second)", "beta"]],
            observed={"finished_while_held": watch["finished_while_held"], "alpha": ra, "beta": rb, "final": final,
                      "writes": writes})
    c.expect(room, "F4", "beta", ["beta"])

    # F5 a real lock error on the waiting connection
    room = new_room(c.main, "f5-waiter-lock-error")
    if env.pg:
        waiter_role, waiter = "second-lt", env.open("second-lt", lock_timeout_ms=1000)
        instrument(waiter, waiter_role, log)
        hold_for, limit = 2.5, 1.0
    else:
        waiter_role, waiter = "second", c.second
        hold_for, limit = 6.5, 5.0
    mark = log.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, held(c.first) as hold:
        a = Op(pool, "F5 join alpha (first, held)", join, c.first, room, "alpha")
        assert hold.read.wait(timeout=HOLD_TIMEOUT)
        b = Op(pool, f"F5 join beta ({waiter_role})", join, waiter, room, "beta")
        watch = watch_while_held(b, env, waiter_role, "first", seconds=hold_for)
        hold.release.set()
        ra, rb = a.outcome(), b.outcome()
    after = view(c.main, room)
    with labelled(f"F5 join beta again ({waiter_role})"):
        join(waiter, room, "beta")
    final = view(c.main, room)
    writes = log.brief(log.since(mark, room))
    if env.pg:
        waiter.close()
    CASES[f"{c.L} F5"] = {"room": room, "waiter": waiter_role, "hold seconds": hold_for, "watch": watch,
                          "alpha": ra, "beta": rb, "beta seconds": b.seconds(), "after": after, "final": final,
                          "writes": writes}
    lock_error = ("database is locked" in rb) if not env.pg else ("lock timeout" in rb or "LockNotAvailable" in rb)
    c.check(f"F5 the waiting join fails with the database's own lock error after waiting ({'busy timeout 5 s' if not env.pg else 'lock_timeout 1 s'})"
            ", and stores nothing", lock_error and (b.seconds() or 0) >= limit * 0.9 and after["agents"] == ["alpha"],
            observed={"beta": rb, "seconds": b.seconds(), "after": after})
    c.check("F5: the held join then commits normally (alpha stored and active); the failed connection joins beta "
            "afterwards and alpha stays active", ra == "ok: 'alpha'" and after["stored"] == "alpha"
            and final == {"stored": "alpha", "active": "alpha", "agents": ["alpha", "beta"]}
            and [w[2] for w in writes] == ["alpha"], observed={"alpha": ra, "final": final, "writes": writes})
    c.expect(room, "F5", "alpha", ["alpha", "beta"])


# ======================================================================= G
def part_g(c: Ctx, rooms: int) -> None:
    log = c.log
    results: dict[str, Any] = {}

    def race(name: str, setup: Any, left_fn: Any, right_fn: Any, judge: Any) -> None:
        counts: dict[str, int] = {"rooms": rooms, "violations": 0, "errors": 0}
        outcomes: dict[str, int] = {}
        examples: list[Any] = []
        for index in range(rooms):
            room = setup(index)
            barrier = threading.Barrier(2)
            mark = log.mark()

            def go(fn: Any) -> Any:
                barrier.wait(HOLD_TIMEOUT)
                return fn(room)

            with ThreadPoolExecutor(max_workers=2) as pool:
                left = Op(pool, f"G {name} first", go, left_fn)
                right = Op(pool, f"G {name} second", go, right_fn)
                lo, ro = left.outcome(), right.outcome()
            key = f"{lo.split(':')[0]} | {ro.split(':')[0]}"
            outcomes[key] = outcomes.get(key, 0) + 1
            raw = c.env.raw_room(room)
            verdict = judge(lo, ro, raw, log.since(mark, room))
            for flag in verdict:
                counts[flag] = counts.get(flag, 0) + 1
            if "violations" in verdict and len(examples) < 3:
                examples.append({"room": room, "first": lo, "second": ro, "raw": [raw["agents"], raw["stored"]],
                                 "writes": log.brief(log.since(mark, room))})
        results[name] = {**counts, "outcomes": outcomes, "examples": examples}
        print(f"  G {name}: {results[name]}", flush=True)

    def setup_room(prefix: str) -> Any:
        return lambda index: new_room(c.main, f"g-{prefix}-{index}")

    def judge_joins(lo: str, ro: str, raw: dict, writes: list) -> list[str]:
        flags = []
        if not (lo.startswith("ok") and ro.startswith("ok")):
            return ["errors"]
        by = {w["by"] for w in writes if w["op"].startswith("G two first joins")}
        if len(by) == 2:
            flags += ["both joins wrote", "violations"]
        seen = {lo[4:].strip("'"), ro[4:].strip("'")}
        if seen != {raw["stored"]}:
            flags += ["a caller saw an agent that was then replaced", "violations"]
        if raw["stored"] not in raw["agents"]:
            flags.append("violations")
        if raw["agents"] and raw["stored"] != raw["agents"][0]:
            flags.append("info: stored is not the first listed")
        return flags

    race("two first joins", setup_room("joins"),
         lambda room: join_and_read_active(c.first, room, "alpha"),
         lambda room: join_and_read_active(c.second, room, "beta"), judge_joins)

    def judge_selection(lo: str, ro: str, raw: dict, writes: list) -> list[str]:
        if not (lo.startswith("ok") and ro.startswith("ok")):
            return ["errors"]
        return [] if raw["stored"] == "beta" else ["selection overwritten", "violations"]

    race("first join vs join and selection", setup_room("selection"),
         lambda room: join(c.first, room, "alpha"),
         lambda room: (join(c.second, room, "beta"), select(c.second, room, "beta"))[1], judge_selection)

    def judge_leave(lo: str, ro: str, raw: dict, writes: list) -> list[str]:
        if not ro.startswith("ok") or not (lo.startswith("ok") or "Failed to upsert participant" in lo):
            return ["errors"]
        stored = (raw["stored"] or "").strip()
        return ["departed active agent", "violations"] if stored and stored not in raw["agents"] else []

    race("first join vs leave of the joining agent", setup_room("leave"),
         lambda room: join(c.first, room, "alpha"),
         lambda room: leave(c.second, room, "alpha"), judge_leave)
    CASES[f"{c.L} G"] = results
    c.check(f"G two first joins, no pause ({rooms} rooms): never both joins write the active agent, no caller sees an "
            "agent that is then replaced, no error",
            results["two first joins"]["violations"] == 0 and results["two first joins"]["errors"] == 0,
            observed={k: v for k, v in results["two first joins"].items() if k != "examples"})
    c.check(f"G first join vs a join plus explicit selection, no pause ({rooms} rooms): the selection is never "
            "overwritten (beta stored in every room), no error",
            results["first join vs join and selection"]["violations"] == 0
            and results["first join vs join and selection"]["errors"] == 0,
            observed={k: v for k, v in results["first join vs join and selection"].items() if k != "examples"})
    c.check(f"G first join vs a leave of the joining agent, no pause ({rooms} rooms): no departed agent is stored "
            "as active, no error", results["first join vs leave of the joining agent"]["violations"] == 0
            and results["first join vs leave of the joining agent"]["errors"] == 0,
            observed={k: v for k, v in results["first join vs leave of the joining agent"].items() if k != "examples"})


# ======================================================================= E
def inspect_child(work: Path, backend: str, manifest: Path, out: Path) -> int:
    """Child process: open a new runtime storage on the same database and read every room."""
    import openminion

    rooms = json.loads(manifest.read_text(encoding="utf-8"))
    if backend == "postgres":
        env = PgEnv.__new__(PgEnv)
        env.base, env.root, env.tag, env.schema = os.environ["P393_BASE"], work, os.environ["P393_TAG"], os.environ["P393_SCHEMA"]
        storage = env.open("reopen")
    else:
        storage = SqliteEnv(work).open("reopen")
    try:
        views = {room: view(storage, room) for room in rooms}
        count = storage.sessions.count_sessions()
    finally:
        storage.close()
    out.write_text(json.dumps({"openminion": [mask(openminion.__file__), openminion.__version__], "pid": os.getpid(),
                               "sessions": count, "views": views}, indent=1), encoding="utf-8")
    return 0


def part_e(checks: Checks, env: Any, label: str, work: Path, backend: str) -> None:
    manifest = work / "rooms.json"
    manifest.write_text(json.dumps(sorted(EXPECT)), encoding="utf-8")
    child_out = work / "reopen.json"
    child_env = {k: v for k, v in os.environ.items()}
    if env.pg:
        child_env.update({"P393_BASE": env.base, "P393_TAG": env.tag, "P393_SCHEMA": env.schema})
    proc = subprocess.run([sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve()), "--inspect", str(work),
                           str(manifest), str(child_out), "--backend", backend],
                          capture_output=True, text=True, env=child_env, timeout=600)
    child = json.loads(child_out.read_text(encoding="utf-8")) if child_out.exists() else {}
    views = child.get("views", {})
    mism_api, mism_raw = [], []
    for room, exp in sorted(EXPECT.items()):
        got = views.get(room)
        want = {"stored": exp["stored"], "active": exp["active"], "agents": exp["agents"]}
        if got != want:
            mism_api.append([exp["case"], room, got, want])
        raw = env.raw_room(room)
        if raw["stored"] != exp["stored"] or raw["agents"] != exp["agents"]:
            mism_raw.append([exp["case"], room, [raw["agents"], raw["stored"]], [exp["agents"], exp["stored"]]])
    sessions, rows = env.raw_all()
    bad = departed_or_unknown_active(sessions, rows)
    CASES[f"{label} E reopen"] = {"child_exit": proc.returncode, "child_stderr_tail": mask(proc.stderr[-800:]),
                                  "child_openminion": child.get("openminion"), "child_pid": child.get("pid"),
                                  "parent_pid": os.getpid(), "sessions_counted_by_child": child.get("sessions"),
                                  "rooms_checked": len(EXPECT), "api_mismatches": mism_api, "raw_mismatches": mism_raw,
                                  "raw_sessions": len(sessions), "raw_participant_rows": len(rows),
                                  "stored_active_not_a_participant": bad}
    if env.pg:
        CASES[f"{label} E reopen"]["server_counts"] = env.counts()
        CASES[f"{label} E reopen"]["server_connections_after_close"] = env.activity()
    checks.check(f"{label} E reopen: a new process opens a new runtime storage on the same database and reads, for every "
                 f"one of the {len(EXPECT)} rooms of parts B-F, the stored and active agent and the agents listed that the "
                 "cases left", proc.returncode == 0 and child.get("pid") != os.getpid() and not mism_api,
                 observed={"child exit": proc.returncode, "mismatches": mism_api[:10],
                           "child openminion": child.get("openminion")})
    checks.check(f"{label} E reopen: read independently of openminion, the stored active_agent_id and the active "
                 "agent rows of every room match", not mism_raw, observed=mism_raw[:10])
    checks.check(f"{label} E reopen: across all {len(sessions)} sessions in the database (stress rooms included), no "
                 "stored active agent is a departed agent or a non-participant", not bad, observed=bad[:10])


# ======================================================================= N (#495 note)
def note_495(env_factory: Any, label: str) -> None:
    """Not a check: #495 (PR #520) is not in 0.1.15. A selection held after its participant check, the
    agent leaves on the other connection, then the selection writes."""
    env = env_factory()
    st = {r: env.open(r) for r in ("main", "first", "second")}
    try:
        room = new_room(st["main"], "n495")
        join(st["main"], room, "alpha")
        join(st["main"], room, "beta")
        owner = st["first"].sessions._participants
        get_participant = owner.get_participant
        checked, go = threading.Event(), threading.Event()

        def held_check(*args: Any, **kwargs: Any) -> Any:
            result = get_participant(*args, **kwargs)
            checked.set()
            go.wait(HOLD_TIMEOUT)
            return result

        with patch.object(owner, "get_participant", held_check), ThreadPoolExecutor(max_workers=1) as pool:
            s = Op(pool, "N select beta (first, held after its check)", select, st["first"], room, "beta")
            checked.wait(HOLD_TIMEOUT)
            left = leave(st["second"], room, "beta")
            go.set()
            rs = s.outcome()
        CASES[f"{label} N #495 note (not a check)"] = {
            "selection": rs, "leave": left, "final": view(st["main"], room),
            "meaning": "#495 is not in 0.1.15; recorded only to show it is still open, not claimed fixed"}
        print(f"NOTE #495 (not a check): {CASES[f'{label} N #495 note (not a check)']}", flush=True)
    finally:
        for s_ in st.values():
            s_.close()
        env.drop()


# ======================================================================= main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("out")
    ap.add_argument("extra", nargs="*")
    ap.add_argument("--backend", default="sqlite", choices=("sqlite", "postgres"))
    ap.add_argument("--rooms", type=int, default=200)
    ap.add_argument("--inspect", action="store_true")
    a = ap.parse_args()
    quiet()
    if a.inspect:  # child process for part E: <work> <manifest> <out>
        return inspect_child(Path(a.work), a.backend, Path(a.out), Path(a.extra[0]))
    base = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()
    if a.backend == "postgres" and not base:
        print("OPENMINION_TEST_POSTGRES_URL is not set: PostgreSQL not run")
        return 2
    work = Path(a.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    for key in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT", "OPENMINION_GENERATED_ROOT", "OPENMINION_CONFIG_PATH"):
        os.environ.pop(key, None)
    info = banner("probe_393", backend=a.backend, rooms_per_stress_case=a.rooms, hold_seconds=HOLD_SECONDS)
    checks = Checks()
    L = a.backend
    env = PgEnv(base, work) if a.backend == "postgres" else SqliteEnv(work)
    if env.pg:
        CASES["postgres"] = env.server()
    c = None
    try:
        c = Ctx(checks, env, L)
        for name, fn in (("A", part_a), ("B1", part_b1), ("B2", part_b2), ("B2b", part_b2b), ("B3", part_b3),
                         ("C", part_c), ("D existing", part_d_existing), ("D blank", part_d_blank),
                         ("D rejoin", part_d_rejoin), ("D legacy", part_d_legacy), ("D order", part_d_order),
                         ("F", part_f)):
            print(f"== {L} {name}", flush=True)
            part(checks, L, name, fn, c)
        print(f"== {L} G", flush=True)
        part(checks, L, "G", part_g, c, a.rooms)
        CASES[f"{L} active-agent writes (all)"] = len(c.log.entries)
    finally:
        if c is not None:
            c.close()
    print(f"== {L} E", flush=True)
    part(checks, L, "E", part_e, checks, env, L, work, a.backend)
    env.drop()
    print(f"== {L} N (#495 note, not a check)", flush=True)
    try:
        note_495(lambda: PgEnv(base, work, tag="n495") if a.backend == "postgres" else SqliteEnv(work / "n495"), L)
    except Exception as exc:  # noqa: BLE001
        CASES[f"{L} N #495 note (not a check)"] = {"error": describe(exc)}
    summary = checks.summary()
    write_result(Path(a.out), {"banner": info, "summary": summary, "checks": checks.items, "expect": EXPECT, "cases": CASES})
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
