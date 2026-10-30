"""#495 (PR #520) on the installed package: a room selection racing a leave of
the agent it selects, on two independent connections, against one SQLite
database or one PostgreSQL schema.

    python -I -X utf8 probe_495.py <work_dir> <out.json> [--backend sqlite|postgres] [--rooms N]

Three runtime storages (`build_runtime_storage`, the package's runtime storage
factory) open the same database: `main` creates rooms and reads, `first` and
`second` are the two independent connections (on PostgreSQL each has its own
engine and application_name, so the server shows which backend waits for
which). Every statement and transaction of their session stores is traced, in
order, with the probe operation that sent it. Each check name starts with the
item of the maintainer's checklist (comment 6494402314) it covers:

  item 1   the installed package, imported outside any checkout;
  item 2   both selection-versus-leave interleavings on independent connections:
           I1 the selection holds the session-row lock (held before its write),
           the leave waits; I2 the leave holds it (held before its fallback),
           the selection waits; I3 the selection held right after its
           membership read;
  item 3   membership validation and the selection write share the transaction
           and the session-row lock joins and leaves take (statement trace T,
           and I1-I3);
  item 4   no committed active agent names a departed participant (I1-I3, an
           unpaused stress G, and every session in the database at E);
  item 5   selection of existing participants (S);
  item 6   invalid/departed selection rejection (V);
  item 7   legacy-owner behavior (L);
  item 8   join/activation compatibility (J; #393's own fix was verified on
           0.1.15 and is not re-claimed here);
  item 9   normal and injected-failure rollback (R);
  item 10  persisted state after reopen (E: a new process, a new runtime
           storage, and reads independent of openminion).

In-process by necessity: the holds and the injected exceptions wrap the
session-store backend's execute_count / query_dicts or the participant owner's
get_active_agent / _get_session; the wrapped call itself is unchanged.
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
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, quiet, write_result  # noqa: E402
from probe_495_lib import (  # noqa: E402
    HOLD_SECONDS,
    HOLD_TIMEOUT,
    Op,
    PgEnv,
    SqliteEnv,
    Trace,
    attempt,
    describe,
    hold_join,
    hold_leave,
    hold_selection,
    instrument,
    join,
    labelled,
    leave,
    mask,
    new_room,
    pg_wait_ok,
    select,
    spanning,
    stored_not_listed,
    tx_summary,
    view,
    watch_while_held,
)

CASES: dict[str, Any] = {}
EXPECT: dict[str, dict[str, Any]] = {}
LEGACY_OWN: dict[str, str] = {}
REFUSED = "not an active participant"
LEAVE_DELAYS = (0.0, 0.0, 0.002, 0.004, 0.006, 0.008, 0.010)


class Ctx:
    def __init__(self, checks: Checks, env: Any, label: str) -> None:
        self.checks, self.env, self.L = checks, env, label
        self.trace = Trace()
        self.st: dict[str, Any] = {}
        for role in ("main", "first", "second"):
            self.st[role] = instrument(env.open(role), role, self.trace)
        self.main, self.first, self.second = self.st["main"], self.st["first"], self.st["second"]

    def check(self, item: int, name: str, ok: bool, observed: Any = None) -> bool:
        return self.checks.check(f"item {item}: [{self.L}] {name}", ok, observed=observed)

    def expect(self, room: str, case: str, stored: Any, agents_: list[str], active: Any = "same") -> None:
        """Record the room for part E: what the case should leave (judged by the case's own checks)
        and what it did leave, read through `main` now; E compares the reopened room with the latter."""
        EXPECT[room] = {"case": case,
                        "ideal": {"stored": stored, "active": stored if active == "same" else active, "agents": agents_},
                        "at_end": view(self.main, room)}

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
        checks.check(f"part {name}: [{label}] completes without an exception", False, observed=describe(exc))


def waited(op: Op, watch: dict[str, Any], entries: list[dict[str, Any]], t_release: float, env: Any) -> dict[str, Any]:
    """Whether `op` waited for the held call: not finished during the hold, one of its calls
    outstanding across the release, and on PostgreSQL its backend seen blocked by the holder's."""
    blocked = spanning(entries, t_release)
    return {
        "ok": (not watch["finished_while_held"] and (op.t_end or 0) >= t_release and blocked is not None
               and pg_wait_ok(env, watch)),
        "finished_while_held": watch["finished_while_held"],
        "seconds": op.seconds(),
        "call_outstanding_across_release": None if blocked is None else
        {"kind": blocked["kind"], "event": blocked.get("event"), "sent_before_release_s": round(t_release - blocked["t_sent"], 3),
         "returned_after_release_s": None if blocked["t_done"] is None else round(blocked["t_done"] - t_release, 3)},
        "pg_lock_wait": (watch["pg_lock_wait"] or {}).get("waiter"),
    }


def selection_shape(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The selection's transactions, and whether its membership read and its write ran in
    one transaction that starts with the session-row lock."""
    txs = tx_summary(entries)
    reads = [e for e in entries if e["kind"] == "membership read"]
    writes = [e for e in entries if e["kind"] == "active-agent write"]
    in_one = [t for t in txs if t["statements"][:1] == ["session-row lock"] and "membership read" in t["statements"]]
    return {
        "transactions": txs,
        "membership_reads": [[e["tx"], e["in_transaction"], e.get("found")] for e in reads],
        "writes": [[e["tx"], e["in_transaction"], e.get("value")] for e in writes],
        "read_and_write_share_a_locked_transaction": bool(in_one) and bool(reads)
        and all(e["tx"] == in_one[-1]["tx"] for e in reads[-1:] + writes),
    }


# ======================================================================= A
def part_a(c: Ctx) -> None:
    info = c.env.describe_connections(c.st)
    CASES[f"{c.L} A connections"] = info
    if c.env.pg:
        CASES[f"{c.L} A server"] = c.env.server()
        ok = (len({v["engine_id"] for v in info.values()}) == 3 and len({v["app"] for v in info.values()}) == 3
              and {v["schema"] for v in info.values()} == {c.env.schema})
        c.check(2, "A: main, first and second are independent PostgreSQL connections (own engine and application_name) "
                "on one schema", ok, observed=info)
    else:
        ok = (len({v["connection_id"] for v in info.values()}) == 3 and len({v["file"] for v in info.values()}) == 1
              and all(v["journal_mode"] == "wal" for v in info.values()))
        c.check(2, "A: main, first and second are independent sqlite3 connections (WAL) on one database file", ok,
                observed=info)


# ======================================================================= T
def part_t(c: Ctx) -> None:
    tr = c.trace
    room = new_room(c.main, "t-trace")
    mark = tr.mark()
    rs = attempt("T select beta (first)", select, c.first, room, "beta")
    rj = attempt("T join gamma (second)", join, c.second, room, "gamma")
    rl = attempt("T leave gamma (second)", leave, c.second, room, "gamma")
    sel = tr.since(mark, op="T select beta (first)")
    shape = selection_shape(sel)
    first_of = {}
    for label in ("T join gamma (second)", "T leave gamma (second)"):
        txs = tx_summary(tr.since(mark, op=label))
        first_of[label] = [t["statements"][:1] for t in txs]
    sel_txs = shape["transactions"]
    CASES[f"{c.L} T"] = {"room": room, "selection": rs, "join": rj, "leave": rl,
                         "selection_trace": Trace.brief(sel), "selection_shape": shape, "first_statement": first_of,
                         "final": view(c.main, room)}
    c.check(3, "T: a selection opens exactly one transaction, which commits, and sends inside it, in order, the "
            "session-row lock, the membership read (found) and the active-agent write; it sends neither outside it",
            rs == "ok: 'beta'" and len(sel_txs) == 1 and sel_txs[0]["outcome"] == "commit"
            and sel_txs[0]["statements"] == ["session-row lock", "membership read", "active-agent write"]
            and shape["read_and_write_share_a_locked_transaction"],
            observed={"selection": rs, "transactions": sel_txs, "membership_reads [tx, in_transaction, found]":
                      shape["membership_reads"], "writes [tx, in_transaction, value]": shape["writes"]})
    c.check(3, "T: that lock statement (`UPDATE sessions SET updated_at = updated_at WHERE id = ?`) is the one the "
            "join's and the leave's transactions start with",
            sel_txs[:1] and sel_txs[0]["statements"][:1] == ["session-row lock"]
            and all(v and all(s == ["session-row lock"] for s in v) for v in first_of.values()),
            observed={"selection": [t["statements"][:1] for t in sel_txs], **first_of})
    c.expect(room, "T", "beta", ["alpha", "beta"])


# ======================================================================= I (both interleavings)
def part_i1(c: Ctx) -> None:
    env, tr = c.env, c.trace
    room = new_room(c.main, "i1-leave-during-selection")
    mark = tr.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, hold_selection(c.first, "before-write") as hold:
        s = Op(pool, "I1 select beta (first, held before its write)", select, c.first, room, "beta")
        assert hold.reached.wait(timeout=HOLD_TIMEOUT)
        during = env.raw_room(room)
        lv = Op(pool, "I1 leave beta (second)", leave, c.second, room, "beta")
        watch = watch_while_held(lv, env, "second", "first")
        t_release = time.monotonic()
        hold.release.set()
        rs, rl = s.outcome(), lv.outcome()
    final, raw = view(c.main, room), env.raw_room(room)
    w = waited(lv, watch, tr.since(mark, op=lv.label), t_release, env)
    shape = selection_shape(tr.since(mark, op=s.label))
    writes = tr.writes(mark, room)
    CASES[f"{c.L} I1"] = {"room": room, "held_in_transaction": hold.in_transaction, "independent_read_while_held": during,
                          "leave_waited": w, "selection": rs, "leave": rl, "final": final, "raw": raw,
                          "selection_shape": shape, "active_agent_writes": writes,
                          "trace": Trace.brief(tr.since(mark), t0=t_release)}
    c.check(2, "I1 (the selection holds the lock): beta's selection on `first` passed its membership read and is held "
            "before its write; beta's leave on `second` waits for it (not finished during the "
            f"{HOLD_SECONDS} s hold, a call outstanding across the release"
            + ("; the server shows the leave's backend blocked on a lock held by the selection's backend)" if env.pg else ")"),
            w["ok"], observed=w)
    c.check(2, "I1: then the selection returns beta and the leave returns True and runs its fallback: agents [alpha], "
            "alpha stored and active; the writes are the selection's beta, then the leave's fallback alpha",
            rs == "ok: 'beta'" and rl == "ok: True" and final == {"stored": "alpha", "active": "alpha", "agents": ["alpha"]}
            and writes == [["first", s.label, "beta"], ["second", lv.label, "alpha"]],
            observed={"selection": rs, "leave": rl, "final": final, "writes": writes})
    c.check(3, "I1: at the hold the selection is inside its transaction, whose first statement is the session-row lock "
            "and which holds its membership read and its write",
            hold.in_transaction is True and shape["read_and_write_share_a_locked_transaction"],
            observed={"in_transaction_at_hold": hold.in_transaction, "transactions": shape["transactions"],
                      "membership_reads [tx, in_transaction, found]": shape["membership_reads"],
                      "writes [tx, in_transaction, value]": shape["writes"]})
    c.check(4, "I1: no committed active agent names a departed participant (read independently: stored alpha is listed, "
            "beta has left)", raw["stored"] in raw["agents"] and "beta" not in raw["agents"],
            observed={"stored": raw["stored"], "agents": raw["agents"], "left": raw["left"]})
    c.expect(room, "I1", "alpha", ["alpha"])


def part_i2(c: Ctx) -> None:
    env, tr = c.env, c.trace
    room = new_room(c.main, "i2-selection-during-leave")
    mark = tr.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, hold_leave(c.second) as hold:
        lv = Op(pool, "I2 leave beta (second, held before its fallback)", leave, c.second, room, "beta")
        assert hold.reached.wait(timeout=HOLD_TIMEOUT)
        during = env.raw_room(room)
        s = Op(pool, "I2 select beta (first)", select, c.first, room, "beta")
        watch = watch_while_held(s, env, "first", "second")
        t_release = time.monotonic()
        hold.release.set()
        rl, rs = lv.outcome(), s.outcome()
    final, raw = view(c.main, room), env.raw_room(room)
    sel = tr.since(mark, op=s.label)
    w = waited(s, watch, sel, t_release, env)
    reads = [e for e in sel if e["kind"] == "membership read"]
    sel_writes = [e for e in sel if e["kind"] == "active-agent write"]
    reads_after = bool(reads) and all(e["t_sent"] >= t_release for e in reads)
    writes_during = [e for e in sel_writes if e["t_sent"] < t_release]
    writes = tr.writes(mark, room)
    CASES[f"{c.L} I2"] = {"room": room, "held_in_transaction": hold.in_transaction, "independent_read_while_held": during,
                          "selection_waited": w, "leave": rl, "selection": rs, "final": final, "raw": raw,
                          "selection_shape": selection_shape(sel), "active_agent_writes": writes,
                          "trace": Trace.brief(tr.since(mark), t0=t_release)}
    c.check(2, "I2 (the leave holds the lock): beta's leave on `second` marked beta as left and is held in its "
            "transaction before its fallback; beta's selection on `first` waits for it (not finished during the "
            f"{HOLD_SECONDS} s hold, a call outstanding across the release"
            + ("; the server shows the selection's backend blocked on a lock held by the leave's backend)" if env.pg else ")"),
            hold.in_transaction is True and w["ok"], observed={"leave_in_transaction_at_hold": hold.in_transaction, **w})
    c.check(2, "I2: then the leave returns True and the selection is refused (ValueError: Agent 'beta' is not an active "
            "participant): agents [alpha], alpha stored and active, no active-agent write",
            rl == "ok: True" and REFUSED in rs and final == {"stored": "alpha", "active": "alpha", "agents": ["alpha"]}
            and writes == [], observed={"leave": rl, "selection": rs, "final": final, "writes": writes})
    c.check(3, "I2: the selection reads beta's membership only after the leave committed (every membership read sent "
            "after the release, finding no row) and sends no active-agent write during the hold",
            reads_after and not any(e.get("found") for e in reads) and not writes_during,
            observed={"membership_reads [sent after release s, found, in_transaction]":
                      [[round(e["t_sent"] - t_release, 3), e.get("found"), e["in_transaction"]] for e in reads],
                      "writes sent during the hold": [[e.get("value"), round(e["t_sent"] - t_release, 3)] for e in writes_during]})
    c.check(4, "I2: no committed active agent names a departed participant (read independently: stored alpha is listed, "
            "beta has left)", raw["stored"] in raw["agents"] and "beta" not in raw["agents"],
            observed={"stored": raw["stored"], "agents": raw["agents"], "left": raw["left"]})
    c.expect(room, "I2", "alpha", ["alpha"])


def part_i3(c: Ctx) -> None:
    env, tr = c.env, c.trace
    room = new_room(c.main, "i3-held-after-membership-read")
    mark = tr.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, hold_selection(c.first, "membership-read") as hold:
        s = Op(pool, "I3 select beta (first, held after its membership read)", select, c.first, room, "beta")
        assert hold.reached.wait(timeout=HOLD_TIMEOUT)
        lv = Op(pool, "I3 leave beta (second)", leave, c.second, room, "beta")
        watch = watch_while_held(lv, env, "second", "first")
        t_release = time.monotonic()
        hold.release.set()
        rs, rl = s.outcome(), lv.outcome()
    final, raw = view(c.main, room), env.raw_room(room)
    w = waited(lv, watch, tr.since(mark, op=lv.label), t_release, env)
    writes = tr.writes(mark, room)
    CASES[f"{c.L} I3"] = {"room": room, "membership_read_found": hold.seen, "held_in_transaction": hold.in_transaction,
                          "leave_waited": w, "selection": rs, "leave": rl, "final": final, "raw": raw,
                          "active_agent_writes": writes, "trace": Trace.brief(tr.since(mark), t0=t_release)}
    c.check(3, "I3: held right after its membership read found beta, the selection is inside its transaction, and "
            "beta's leave on `second` waits for it" + (" (server: blocked on the selection's lock)" if env.pg else "")
            + "; the leave then applies after the selection's write: agents [alpha], alpha stored",
            hold.seen is True and hold.in_transaction is True and w["ok"] and rs == "ok: 'beta'" and rl == "ok: True"
            and final == {"stored": "alpha", "active": "alpha", "agents": ["alpha"]},
            observed={"read found": hold.seen, "in_transaction_at_hold": hold.in_transaction, "leave_waited": w,
                      "selection": rs, "leave": rl, "final": final, "writes": writes})
    c.check(4, "I3: no committed active agent names a departed participant (read independently)",
            raw["stored"] in raw["agents"] and "beta" not in raw["agents"],
            observed={"stored": raw["stored"], "agents": raw["agents"], "left": raw["left"]})
    c.expect(room, "I3", "alpha", ["alpha"])


# ======================================================================= S
def part_s(c: Ctx) -> None:
    env, tr = c.env, c.trace
    room = new_room(c.main, "s-existing", ("alpha",))
    join(c.first, room, "beta")
    join(c.second, room, "gamma")
    fresh = instrument(env.open("fresh"), "fresh", tr)
    steps = [
        ("second selects beta (joined on first)", c.second, "beta", "beta"),
        ("first selects gamma (joined on second)", c.first, "gamma", "gamma"),
        ("main selects '  ALPHA ' (normalized to alpha)", c.main, "  ALPHA ", "alpha"),
        ("second selects alpha again (already the active agent)", c.second, "alpha", "alpha"),
        ("a storage opened after the joins selects beta", fresh, "beta", "beta"),
    ]
    timeline, ok = [], True
    for name, storage, agent, expected in steps:
        mark = tr.mark()
        result = attempt(f"S {name}", select, storage, room, agent)
        seen = {"returned": result, "raw": env.raw_room(room)["stored"],
                "main": c.main.sessions.get_active_agent(room), "first": c.first.sessions.get_active_agent(room),
                "second": c.second.sessions.get_active_agent(room)}
        writes = tr.writes(mark, room)
        good = (result == f"ok: {expected!r}" and seen["raw"] == expected
                and seen["main"] == seen["first"] == seen["second"] == expected and len(writes) == 1)
        ok = ok and good
        timeline.append({"step": name, **seen, "writes": writes, "ok": good})
    fresh.close()
    # a departed agent that rejoins (on the other connection) can be selected again
    leave(c.first, room, "gamma")
    join(c.second, room, "gamma")
    rejoin = attempt("S first selects gamma after it left and rejoined", select, c.first, room, "gamma")
    final = view(c.main, room)
    CASES[f"{c.L} S"] = {"room": room, "timeline": timeline, "rejoined selection": rejoin, "final": final}
    c.check(5, "S: selections of existing participants apply across connections: an agent joined on the other "
            "connection, the current active agent again, a mixed-case/padded id (normalized), and from a storage "
            "opened after the joins; each returns the agent, is stored, read by all three connections, one write each",
            ok, observed=[[t["step"], t["returned"], t["raw"], t["ok"]] for t in timeline])
    c.check(5, "S: an agent that left and rejoined on the other connection can be selected again",
            rejoin == "ok: 'gamma'" and final == {"stored": "gamma", "active": "gamma", "agents": ["alpha", "beta", "gamma"]},
            observed={"selection": rejoin, "final": final})
    c.expect(room, "S", "gamma", ["alpha", "beta", "gamma"])


# ======================================================================= V
def part_v(c: Ctx) -> None:
    env, tr = c.env, c.trace
    room = new_room(c.main, "v-rejections")
    join(c.second, room, "delta")
    leave(c.second, room, "delta")
    before = env.raw_room(room)
    attempts = [
        ("zeta (never a participant)", room, "zeta", REFUSED),
        ("delta (left on the other connection)", room, "delta", REFUSED),
        ("owner (a human participant, not an agent)", room, "owner", REFUSED),
        ("'' (empty id)", room, "", "agent_id is required"),
        ("'   ' (spaces only)", room, "   ", "agent_id is required"),
        ("alpha in a session that does not exist", "no-such-session-495", "alpha", "Session not found"),
    ]
    results, ok = [], True
    for name, session_id, agent, expected_error in attempts:
        mark = tr.mark()
        label = f"V first selects {name}"
        result = attempt(label, select, c.first, session_id, agent)
        entries = tr.since(mark, op=label)
        raw = env.raw_room(room)
        in_tx = bool(c.first.record_store.in_transaction)
        locks = env.locks_held("first")
        writes = [e for e in entries if e["kind"] == "active-agent write"]
        good = (expected_error in result and raw["stored"] == "alpha" and raw["agents"] == ["alpha", "beta"]
                and not writes and not in_tx and locks == 0)
        ok = ok and good
        results.append({"attempt": name, "result": result, "stored_after": raw["stored"], "agents_after": raw["agents"],
                        "active_agent_writes": len(writes), "first_in_transaction_after": in_tx,
                        "pg_locks_held_by_first": locks, "transactions": tx_summary(entries), "ok": good})
    t0 = time.monotonic()
    after = attempt("V second selects beta after the refusals", select, c.second, room, "beta")
    seconds = round(time.monotonic() - t0, 3)
    final = view(c.main, room)
    CASES[f"{c.L} V"] = {"room": room, "before": before, "attempts": results, "second selects beta": after,
                         "seconds": seconds, "final": final}
    c.check(6, "V: refused on `first`, each with its error and with nothing written (stored alpha, agents [alpha, beta], "
            "no active-agent write, `first` not left in a transaction" + (", no lock held on the server" if env.pg else "")
            + "): an agent that never joined, an agent that left on the other connection, the human owner's id, an "
            "empty and a spaces-only id (agent_id is required), an unknown session (Session not found)",
            ok and before["left"] == [["agent", "delta", 4]],
            observed=[[r["attempt"], r["result"], r["stored_after"], r["active_agent_writes"],
                       r["first_in_transaction_after"], r["pg_locks_held_by_first"]] for r in results])
    c.check(6, "V: after the refusals the lock is free: a selection of beta on `second` completes at once (under 1 s) "
            "and applies", after == "ok: 'beta'" and seconds < 1.0
            and final == {"stored": "beta", "active": "beta", "agents": ["alpha", "beta"]},
            observed={"selection": after, "seconds": seconds, "final": final})
    c.expect(room, "V", "beta", ["alpha", "beta"])


# ======================================================================= L
def part_l(c: Ctx) -> None:
    env, tr = c.env, c.trace
    tag = uuid.uuid4().hex[:6]
    # L0: the session's own agent selected before any participant record exists
    s0 = c.main.sessions.resolve_session(agent_id="legacy-agent", channel="console", target=f"legacy-own-{tag}").id
    LEGACY_OWN[s0] = "legacy-agent"
    raw0 = env.raw_room(s0)
    mark = tr.mark()
    r0 = attempt("L0 first selects legacy-agent (no participant record yet)", select, c.first, s0, "legacy-agent")
    txs0 = tx_summary(tr.since(mark, op="L0 first selects legacy-agent (no participant record yet)"))
    v0 = env.raw_room(s0)
    c.expect(s0, "L0", "legacy-agent", ["legacy-agent"])

    # L1..L6 on another legacy session
    s1 = c.main.sessions.resolve_session(agent_id="legacy-agent", channel="console", target=f"legacy-rules-{tag}").id
    LEGACY_OWN[s1] = "legacy-agent"
    mark = tr.mark()
    r1 = attempt("L1 first selects guest-agent (not a participant)", select, c.first, s1, "guest-agent")
    v1 = env.raw_room(s1)
    w1 = tr.writes(mark, s1)
    join(c.second, s1, "guest-agent")
    v2 = env.raw_room(s1)
    r3 = attempt("L3 first selects guest-agent (joined)", select, c.first, s1, "guest-agent")
    r4 = attempt("L4 second selects legacy-agent (the session's own agent)", select, c.second, s1, "legacy-agent")
    leave(c.first, s1, "legacy-agent")
    v5 = env.raw_room(s1)
    r6 = attempt("L6 second selects legacy-agent after its owner record left", select, c.second, s1, "legacy-agent")
    v6 = env.raw_room(s1)
    r7 = attempt("L7 first selects other-agent (not a participant)", select, c.first, s1, "other-agent")
    v7 = env.raw_room(s1)
    CASES[f"{c.L} L"] = {
        "L0": {"session": s0, "raw_before": raw0, "selection": r0, "raw_after": v0, "transactions": txs0},
        "L1-L7": {"session": s1, "L1 select guest-agent": r1, "after L1": v1, "L1 writes": w1,
                  "after guest-agent joins": v2, "L3 select guest-agent": r3, "L4 select legacy-agent": r4,
                  "after legacy-agent leaves": v5, "L6 select legacy-agent": r6, "after L6": v6,
                  "L7 select other-agent": r7, "after L7": v7},
    }
    c.check(7, "L0: in a legacy session with no participant record, the session's own agent can be selected: its owner "
            "record is written and the selection applies (stored legacy-agent)",
            raw0["participants"] == [] and r0 == "ok: 'legacy-agent'" and v0["stored"] == "legacy-agent"
            and v0["participants"] == [["agent", "legacy-agent", "owner", 1]],
            observed={"before": raw0["participants"], "selection": r0, "after": [v0["participants"], v0["stored"]]})
    c.check(7, "L0: the owner record is written in its own transaction before the selection's transaction, which takes "
            "the session-row lock, reads the membership and writes the active agent",
            [t["statements"] for t in txs0] == [["session-row lock", "participant row write"],
                                                ["session-row lock", "membership read", "active-agent write"]]
            and all(t["outcome"] == "commit" for t in txs0), observed=txs0)
    c.check(7, "L1: another agent is refused before it joins (Legacy session ... is bound to agent 'legacy-agent'), "
            "storing no active agent; the owner record is listed",
            "is bound to agent 'legacy-agent'" in r1 and v1["stored"] is None and w1 == []
            and v1["participants"] == [["agent", "legacy-agent", "owner", 1]],
            observed={"selection": r1, "stored": v1["stored"], "participants": v1["participants"], "writes": w1})
    c.check(7, "L3/L4: once it joined (on the other connection, activating it in a session with no stored agent), "
            "guest-agent can be selected, and so can the session's own agent",
            v2["stored"] == "guest-agent" and r3 == "ok: 'guest-agent'" and r4 == "ok: 'legacy-agent'",
            observed={"after join": v2["stored"], "L3": r3, "L4": r4})
    c.check(7, "L5/L6: the own agent leaves (fallback to guest-agent); the session's own agent can still be selected "
            "after its owner record left, as the legacy rule allows (unchanged, upstream's "
            "check_legacy_session_selection)",
            v5["agents"] == ["guest-agent"] and v5["stored"] == "guest-agent" and r6 == "ok: 'legacy-agent'"
            and v6["stored"] == "legacy-agent", observed={"after leave": [v5["agents"], v5["stored"]], "L6": r6,
                                                          "after L6": [v6["agents"], v6["stored"]]})
    c.check(7, "L7: an agent that is not a participant is still refused (bound to agent 'legacy-agent'), and the "
            "stored agent is unchanged", "is bound to agent 'legacy-agent'" in r7 and v7["stored"] == "legacy-agent",
            observed={"selection": r7, "stored": v7["stored"]})
    c.expect(s1, "L1-L7", "legacy-agent", ["guest-agent"])


# ======================================================================= J
def part_j(c: Ctx) -> None:
    env, tr = c.env, c.trace
    # J1 first join activates; later joins keep it
    room = new_room(c.main, "j1-first-join", ())
    mark = tr.mark()
    with labelled("J1 first joins alpha"):
        join(c.first, room, "alpha")
    with labelled("J1 second joins beta"):
        join(c.second, room, "beta")
    j1 = view(c.main, room)
    w1 = tr.writes(mark, room)
    c.check(8, "J1: the first join into a room with no active agent activates it (alpha); a later join on the other "
            "connection keeps it; one write", j1 == {"stored": "alpha", "active": "alpha", "agents": ["alpha", "beta"]}
            and w1 == [["first", "J1 first joins alpha", "alpha"]], observed={"final": j1, "writes": w1})
    c.expect(room, "J1", "alpha", ["alpha", "beta"])

    # J2 a join while a selection is held
    room = new_room(c.main, "j2-join-during-selection")
    mark = tr.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, hold_selection(c.first, "before-write") as hold:
        s = Op(pool, "J2 select beta (first, held before its write)", select, c.first, room, "beta")
        assert hold.reached.wait(timeout=HOLD_TIMEOUT)
        j = Op(pool, "J2 join gamma (second)", join, c.second, room, "gamma")
        watch = watch_while_held(j, env, "second", "first")
        t_release = time.monotonic()
        hold.release.set()
        rs, rj = s.outcome(), j.outcome()
    j2 = view(c.main, room)
    w = waited(j, watch, tr.since(mark, op=j.label), t_release, env)
    w2 = tr.writes(mark, room)
    CASES[f"{c.L} J2"] = {"room": room, "join_waited": w, "selection": rs, "join": rj, "final": j2, "writes": w2}
    c.check(8, "J2: a join on `second` waits for a selection held on `first`"
            + (" (server: blocked on the selection's lock)" if env.pg else ""), w["ok"], observed=w)
    c.check(8, "J2: the join then adds gamma without writing the active agent; the selection's beta stays",
            rs == "ok: 'beta'" and rj == "ok: 'gamma'"
            and j2 == {"stored": "beta", "active": "beta", "agents": ["alpha", "beta", "gamma"]}
            and w2 == [["first", s.label, "beta"]], observed={"selection": rs, "join": rj, "final": j2, "writes": w2})
    c.expect(room, "J2", "beta", ["alpha", "beta", "gamma"])

    # J3 a join plus selection while a first join is held at its activation read (#393's B2 shape)
    room = new_room(c.main, "j3-selection-during-join", ())

    def join_and_select_beta() -> Any:
        join(c.second, room, "beta")
        return select(c.second, room, "beta")

    mark = tr.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, hold_join(c.first) as hold:
        a = Op(pool, "J3 join alpha (first, held at its activation read)", join, c.first, room, "alpha")
        assert hold.reached.wait(timeout=HOLD_TIMEOUT)
        b = Op(pool, "J3 join beta + select beta (second)", join_and_select_beta)
        watch = watch_while_held(b, env, "second", "first")
        t_release = time.monotonic()
        hold.release.set()
        ra, rb = a.outcome(), b.outcome()
    j3 = view(c.main, room)
    w = waited(b, watch, tr.since(mark, op=b.label), t_release, env)
    w3 = tr.writes(mark, room)
    CASES[f"{c.L} J3"] = {"room": room, "held_read_saw": hold.seen, "waited": w, "alpha": ra, "selection": rb,
                          "final": j3, "writes": w3}
    c.check(8, "J3: with a first join held at its activation read, a join plus selection on the other connection waits, "
            "then the selection applies over the first activation (writes: alpha's activation, then beta)",
            hold.seen is None and w["ok"] and ra == "ok: 'alpha'" and rb == "ok: 'beta'"
            and j3 == {"stored": "beta", "active": "beta", "agents": ["alpha", "beta"]}
            and w3 == [["first", a.label, "alpha"], ["second", b.label, "beta"]],
            observed={"waited": w, "alpha": ra, "selection": rb, "final": j3, "writes": w3})
    c.expect(room, "J3", "beta", ["alpha", "beta"])

    # J4 overlapping first joins (#393's B1 shape)
    room = new_room(c.main, "j4-two-first-joins", ())
    mark = tr.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, hold_join(c.first) as hold:
        a = Op(pool, "J4 join alpha (first, held)", join, c.first, room, "alpha")
        assert hold.reached.wait(timeout=HOLD_TIMEOUT)
        b = Op(pool, "J4 join beta (second)", join, c.second, room, "beta")
        watch = watch_while_held(b, env, "second", "first")
        t_release = time.monotonic()
        hold.release.set()
        ra, rb = a.outcome(), b.outcome()
    j4 = view(c.main, room)
    w = waited(b, watch, tr.since(mark, op=b.label), t_release, env)
    w4 = tr.writes(mark, room)
    c.check(8, "J4: two overlapping first joins: the second waits; the first committed activation (alpha) stays; one "
            "write", w["ok"] and j4 == {"stored": "alpha", "active": "alpha", "agents": ["alpha", "beta"]}
            and w4 == [["first", a.label, "alpha"]], observed={"waited": w, "final": j4, "writes": w4})
    c.expect(room, "J4", "alpha", ["alpha", "beta"])

    # J5 leave of the active agent falls back; rejoin; selection of the rejoined agent
    room = new_room(c.main, "j5-fallback-rejoin", ("alpha", "beta", "gamma"))
    steps = [
        ("first selects gamma", lambda: select(c.first, room, "gamma"), "gamma"),
        ("second: gamma (the active agent) leaves", lambda: leave(c.second, room, "gamma"), "alpha"),
        ("first: gamma rejoins", lambda: join(c.first, room, "gamma"), "alpha"),
        ("second selects gamma", lambda: select(c.second, room, "gamma"), "gamma"),
        ("first: alpha (not active) leaves", lambda: leave(c.first, room, "alpha"), "gamma"),
    ]
    timeline, ok = [], True
    for name, action, expected in steps:
        with labelled(f"J5 {name}"):
            result = action()
        seen = view(c.main, room)
        good = seen["stored"] == expected
        ok = ok and good
        timeline.append([name, repr(result), seen, expected, good])
    raw = env.raw_room(room)
    c.check(8, "J5: a leave of the active agent falls back to the first listed agent, a rejoin keeps the active agent "
            "and is listed last, the rejoined agent can be selected, a leave of another agent changes nothing",
            ok and raw["agents"] == ["beta", "gamma"], observed={"timeline": [[t[0], t[2]["stored"], t[4]] for t in timeline],
                                                                  "agents": raw["agents"]})
    c.expect(room, "J5", "gamma", ["beta", "gamma"])


# ======================================================================= R
def part_r(c: Ctx) -> None:
    env, tr = c.env, c.trace
    # R1 normal path: the write is visible only at commit
    room = new_room(c.main, "r1-commit")
    with ThreadPoolExecutor(max_workers=1) as pool, hold_selection(c.first, "after-write") as hold:
        s = Op(pool, "R1 select beta (first, held after its write)", select, c.first, room, "beta")
        assert hold.reached.wait(timeout=HOLD_TIMEOUT)
        during = env.raw_room(room)
        hold.release.set()
        rs = s.outcome()
    after = env.raw_room(room)
    CASES[f"{c.L} R1"] = {"room": room, "in_transaction_at_hold": hold.in_transaction, "during": during,
                          "selection": rs, "after": after}
    c.check(9, "R1 normal path: held right after its write, the selection is still in its transaction and an "
            "independent reader still sees alpha; after it returns (commit), beta",
            hold.in_transaction is True and during["stored"] == "alpha" and rs == "ok: 'beta'" and after["stored"] == "beta",
            observed={"in_transaction": hold.in_transaction, "stored during": during["stored"], "selection": rs,
                      "stored after": after["stored"]})
    c.expect(room, "R1", "beta", ["alpha", "beta"])

    # R2 injected failure after the write
    room = new_room(c.main, "r2-fail-after-write")
    mark = tr.mark()
    with hold_selection(c.first, "after-write", fail=RuntimeError("probe: injected failure after the selection write")) as hold:
        hold.release.set()
        r2 = attempt("R2 select beta (first, fails after its write)", select, c.first, room, "beta")
    entries = tr.since(mark, op="R2 select beta (first, fails after its write)")
    raw = env.raw_room(room)
    in_tx = bool(c.first.record_store.in_transaction)
    locks = env.locks_held("first")
    t0 = time.monotonic()
    rl = attempt("R2 leave beta (second, after the failure)", leave, c.second, room, "beta")
    seconds = round(time.monotonic() - t0, 3)
    final = view(c.main, room)
    CASES[f"{c.L} R2"] = {"room": room, "selection": r2, "transactions": tx_summary(entries), "raw_after_failure": raw,
                          "first_in_transaction": in_tx, "pg_locks_held_by_first": locks, "leave": rl,
                          "leave_seconds": seconds, "final": final}
    c.check(9, "R2 injected failure after the selection's write: the selection raises it, its transaction rolls back, "
            "and alpha is still stored (read independently); `first` is not left in a transaction"
            + (" and holds no lock on the server" if env.pg else ""),
            "injected failure after the selection write" in r2 and raw["stored"] == "alpha" and not in_tx and locks == 0,
            observed={"selection": r2, "stored after failure": raw["stored"], "transactions": tx_summary(entries),
                      "in_transaction": in_tx, "locks": locks})
    c.check(9, "R2: a leave of beta on `second` then completes at once (under 1 s): agents [alpha], alpha stored",
            rl == "ok: True" and seconds < 1.0 and final == {"stored": "alpha", "active": "alpha", "agents": ["alpha"]},
            observed={"leave": rl, "seconds": seconds, "final": final})
    c.expect(room, "R2", "alpha", ["alpha"])

    # R3 injected failure at the membership read
    room = new_room(c.main, "r3-fail-at-read")
    mark = tr.mark()
    with hold_selection(c.first, "membership-read", fail=RuntimeError("probe: injected failure at the membership read")) as hold:
        hold.release.set()
        r3 = attempt("R3 select beta (first, fails at its membership read)", select, c.first, room, "beta")
    entries = tr.since(mark, op="R3 select beta (first, fails at its membership read)")
    raw = env.raw_room(room)
    in_tx = bool(c.first.record_store.in_transaction)
    locks = env.locks_held("first")
    t0 = time.monotonic()
    rs = attempt("R3 select beta (second, after the failure)", select, c.second, room, "beta")
    seconds = round(time.monotonic() - t0, 3)
    final = view(c.main, room)
    CASES[f"{c.L} R3"] = {"room": room, "selection": r3, "transactions": tx_summary(entries), "raw_after_failure": raw,
                          "first_in_transaction": in_tx, "pg_locks_held_by_first": locks, "second": rs,
                          "seconds": seconds, "final": final}
    c.check(9, "R3 injected failure at the membership read: the selection raises it and writes nothing (alpha stored); "
            "`first` is not left in a transaction" + (", holds no lock on the server" if env.pg else "")
            + "; a selection of beta on `second` then completes at once and applies",
            "injected failure at the membership read" in r3 and raw["stored"] == "alpha"
            and not any(e["kind"] == "active-agent write" for e in entries) and not in_tx and locks == 0
            and rs == "ok: 'beta'" and seconds < 1.0 and final["stored"] == "beta",
            observed={"selection": r3, "stored after failure": raw["stored"], "transactions": tx_summary(entries),
                      "in_transaction": in_tx, "locks": locks, "second": rs, "seconds": seconds})
    c.expect(room, "R3", "beta", ["alpha", "beta"])

    # R4 injected failure in a held selection while a leave waits
    room = new_room(c.main, "r4-selection-fails-while-leave-waits")
    mark = tr.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, \
            hold_selection(c.first, "before-write", fail=RuntimeError("probe: injected failure in the held selection")) as hold:
        s = Op(pool, "R4 select beta (first, held, then fails)", select, c.first, room, "beta")
        assert hold.reached.wait(timeout=HOLD_TIMEOUT)
        lv = Op(pool, "R4 leave beta (second)", leave, c.second, room, "beta")
        watch = watch_while_held(lv, env, "second", "first")
        t_release = time.monotonic()
        hold.release.set()
        rs, rl = s.outcome(), lv.outcome()
    final = view(c.main, room)
    w = waited(lv, watch, tr.since(mark, op=lv.label), t_release, env)
    writes = tr.writes(mark, room)
    CASES[f"{c.L} R4"] = {"room": room, "leave_waited": w, "selection": rs, "leave": rl, "final": final, "writes": writes}
    c.check(9, "R4: a leave of beta on `second` waits for a selection of beta held on `first`"
            + (" (server: blocked on the selection's lock)" if env.pg else ""), w["ok"], observed=w)
    c.check(9, "R4: the held selection then fails with an injected exception and writes nothing; the leave applies: "
            "agents [alpha], alpha stored, no active-agent write",
            "injected failure in the held selection" in rs and rl == "ok: True"
            and final == {"stored": "alpha", "active": "alpha", "agents": ["alpha"]} and writes == [],
            observed={"selection": rs, "leave": rl, "final": final, "writes": writes})
    c.expect(room, "R4", "alpha", ["alpha"])

    # R5 injected failure in a held leave while a selection waits
    room = new_room(c.main, "r5-leave-fails-while-selection-waits")
    mark = tr.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, \
            hold_leave(c.second, fail=RuntimeError("probe: injected failure in the held leave")) as hold:
        lv = Op(pool, "R5 leave beta (second, held, then fails)", leave, c.second, room, "beta")
        assert hold.reached.wait(timeout=HOLD_TIMEOUT)
        s = Op(pool, "R5 select beta (first)", select, c.first, room, "beta")
        watch = watch_while_held(s, env, "first", "second")
        t_release = time.monotonic()
        hold.release.set()
        rl, rs = lv.outcome(), s.outcome()
    final, raw = view(c.main, room), env.raw_room(room)
    sel = tr.since(mark, op=s.label)
    w = waited(s, watch, sel, t_release, env)
    CASES[f"{c.L} R5"] = {"room": room, "selection_waited": w, "leave": rl, "selection": rs, "final": final, "raw": raw,
                          "selection_reads [sent after release s, found]":
                          [[round(e["t_sent"] - t_release, 3), e.get("found")] for e in sel if e["kind"] == "membership read"]}
    c.check(9, "R5: a selection of beta on `first` waits for a leave of beta held on `second`"
            + (" (server: blocked on the leave's lock)" if env.pg else "")
            + "; the leave then fails with an injected exception and rolls back (beta still listed, no left_at); the "
            "selection applies: beta stored and listed",
            w["ok"] and "injected failure in the held leave" in rl and rs == "ok: 'beta'" and raw["left"] == []
            and final == {"stored": "beta", "active": "beta", "agents": ["alpha", "beta"]},
            observed={"waited": w, "leave": rl, "selection": rs, "final": final, "left": raw["left"]})
    c.expect(room, "R5", "beta", ["alpha", "beta"])

    # R6 a real lock error on the waiting selection
    room = new_room(c.main, "r6-selection-lock-error")
    if env.pg:
        waiter_role = "first-lt"
        waiter = instrument(env.open("first-lt", lock_timeout_ms=1000), waiter_role, tr)
        hold_for, limit = 2.5, 1.0
    else:
        waiter_role, waiter = "first", c.first
        hold_for, limit = 6.5, 5.0
    mark = tr.mark()
    with ThreadPoolExecutor(max_workers=2) as pool, hold_leave(c.second) as hold:
        lv = Op(pool, "R6 leave beta (second, held)", leave, c.second, room, "beta")
        assert hold.reached.wait(timeout=HOLD_TIMEOUT)
        s = Op(pool, f"R6 select alpha ({waiter_role})", select, waiter, room, "alpha")
        watch = watch_while_held(s, env, waiter_role, "second", seconds=hold_for)
        hold.release.set()
        rl, rs = lv.outcome(), s.outcome()
    entries = tr.since(mark, op=s.label)
    in_tx = bool(waiter.record_store.in_transaction)
    after = view(c.main, room)
    again = attempt(f"R6 select alpha again ({waiter_role})", select, waiter, room, "alpha")
    final = view(c.main, room)
    if env.pg:
        waiter.close()
    lock_error = ("database is locked" in rs) if not env.pg else ("lock timeout" in rs or "LockNotAvailable" in rs)
    CASES[f"{c.L} R6"] = {"room": room, "waiter": waiter_role, "hold_seconds": hold_for, "watch": watch,
                          "selection": rs, "selection_seconds": s.seconds(), "transactions": tx_summary(entries),
                          "waiter_in_transaction_after": in_tx, "leave": rl, "after": after, "again": again,
                          "final": final}
    c.check(9, "R6: a selection waiting on a held leave fails with the database's own lock error after waiting ("
            + ("busy timeout 5 s" if not env.pg else "lock_timeout 1 s") + "), writes nothing and leaves its connection "
            "out of any transaction; the leave then commits, and the same connection selects alpha afterwards",
            lock_error and (s.seconds() or 0) >= limit * 0.9 and not in_tx and rl == "ok: True"
            and not any(e["kind"] == "active-agent write" and "error" not in e for e in entries)
            and after == {"stored": "alpha", "active": "alpha", "agents": ["alpha"]} and again == "ok: 'alpha'",
            observed={"selection": rs, "seconds": s.seconds(), "in_transaction": in_tx, "leave": rl, "after": after,
                      "again": again})
    c.expect(room, "R6", "alpha", ["alpha"])


# ======================================================================= G
def part_g(c: Ctx, rooms: int) -> None:
    tr, env = c.trace, c.env
    counts: dict[str, int] = {"rooms": rooms, "violations": 0, "errors": 0, "selection applied, then the leave fell back": 0,
                              "selection refused after the leave": 0, "other": 0}
    outcomes: dict[str, int] = {}
    by_delay: dict[str, dict[str, int]] = {}
    examples: list[Any] = []
    for index in range(rooms):
        room = new_room(c.main, f"g-{index}")
        barrier = threading.Barrier(2)
        # No hold: both calls are released together; the leave starts 0-10 ms later (cycled per
        # room), so both orders occur on both backends.
        delay = LEAVE_DELAYS[index % len(LEAVE_DELAYS)]

        def go(fn: Any, *args: Any, wait: float = 0.0) -> Any:
            barrier.wait(HOLD_TIMEOUT)
            if wait:
                time.sleep(wait)
            return fn(*args)

        mark = tr.mark()
        with ThreadPoolExecutor(max_workers=2) as pool:
            s = Op(pool, "G select beta (first)", go, select, c.first, room, "beta")
            lv = Op(pool, "G leave beta (second)", go, leave, c.second, room, "beta", wait=delay)
            rs, rl = s.outcome(), lv.outcome()
        raw = env.raw_room(room)
        key = f"{rs.split(':')[0]} | {rl}"
        outcomes[key] = outcomes.get(key, 0) + 1
        bucket = by_delay.setdefault(f"leave delayed {int(delay * 1000)} ms", {})
        verdict = ("departed agent stored" if raw["stored"] not in raw["agents"] else
                   "selection applied first" if rs == "ok: 'beta'" else "selection refused" if REFUSED in rs else "other")
        bucket[verdict] = bucket.get(verdict, 0) + 1
        writes = tr.writes(mark, room)
        if not rl == "ok: True" or not (rs == "ok: 'beta'" or REFUSED in rs):
            counts["errors"] += 1
        elif raw["stored"] not in raw["agents"]:
            counts["violations"] += 1
            if len(examples) < 3:
                examples.append({"room": room, "selection": rs, "leave": rl, "raw": [raw["agents"], raw["stored"]],
                                 "writes": writes})
        elif rs == "ok: 'beta'" and raw["stored"] == "alpha":
            counts["selection applied, then the leave fell back"] += 1
        elif REFUSED in rs and raw["stored"] == "alpha":
            counts["selection refused after the leave"] += 1
        else:
            counts["other"] += 1
        c.expect(room, "G", raw["stored"], raw["agents"])
    CASES[f"{c.L} G"] = {**counts, "outcomes": outcomes, "by_leave_delay": by_delay, "examples": examples}
    print(f"  G: {CASES[f'{c.L} G']}", flush=True)
    c.check(4, f"G unpaused stress ({rooms} rooms, selection of beta on `first` and leave of beta on `second` released "
            "together from a barrier, the leave 0-10 ms later): no room stores a departed agent, no unexpected error; "
            "each room ends either with the selection applied and then the leave's fallback, or with the selection "
            "refused after the leave",
            counts["violations"] == 0 and counts["errors"] == 0 and counts["other"] == 0,
            observed={k: v for k, v in CASES[f"{c.L} G"].items()})


# ======================================================================= E
def inspect_child(work: Path, backend: str, manifest: Path, out: Path) -> int:
    """Child process: open a new runtime storage on the same database and read every room."""
    import openminion

    rooms = json.loads(manifest.read_text(encoding="utf-8"))
    if backend == "postgres":
        env = PgEnv.__new__(PgEnv)
        env.base, env.root, env.tag, env.schema = os.environ["P495_BASE"], work, os.environ["P495_TAG"], os.environ["P495_SCHEMA"]
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
    child_env = dict(os.environ)
    if env.pg:
        child_env.update({"P495_BASE": env.base, "P495_TAG": env.tag, "P495_SCHEMA": env.schema})
    proc = subprocess.run([sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve()), "--inspect", str(work),
                           str(manifest), str(child_out), "--backend", backend],
                          capture_output=True, text=True, env=child_env, timeout=600)
    child = json.loads(child_out.read_text(encoding="utf-8")) if child_out.exists() else {}
    views = child.get("views", {})
    mism_api, mism_raw = [], []
    for room, exp in sorted(EXPECT.items()):
        got = views.get(room)
        want = exp["at_end"]
        if got != want:
            mism_api.append([exp["case"], room, got, want])
        raw = env.raw_room(room)
        if raw["stored"] != want["stored"] or raw["agents"] != want["agents"]:
            mism_raw.append([exp["case"], room, [raw["agents"], raw["stored"]], [want["agents"], want["stored"]]])
    sessions, rows = env.raw_all()
    bad, legacy = stored_not_listed(sessions, rows, LEGACY_OWN)
    named = {k: v["case"] for k, v in EXPECT.items() if v["case"] != "G"}
    off_ideal = [[v["case"], k, v["at_end"], v["ideal"]] for k, v in sorted(EXPECT.items())
                 if v["case"] != "G" and v["at_end"] != v["ideal"]]
    CASES[f"{label} E reopen"] = {"child_exit": proc.returncode, "child_stderr_tail": mask(proc.stderr[-800:]),
                                  "child_openminion": child.get("openminion"), "child_pid": child.get("pid"),
                                  "parent_pid": os.getpid(), "sessions_counted_by_child": child.get("sessions"),
                                  "rooms_checked": len(EXPECT), "named_cases": len(named), "api_mismatches": mism_api,
                                  "raw_mismatches": mism_raw, "raw_sessions": len(sessions),
                                  "raw_participant_rows": len(rows), "stored_active_not_a_participant": bad,
                                  "legacy_sessions_storing_their_own_agent": legacy,
                                  "case_rooms_whose_end_state_differs_from_the_fixed_behavior": off_ideal}
    if env.pg:
        CASES[f"{label} E reopen"]["server_counts"] = env.counts()
        CASES[f"{label} E reopen"]["server_connections_after_close"] = env.activity()
    checks.check(f"item 10: [{label}] E reopen: a new process opens a new runtime storage on the same database and "
                 f"reads, for every one of the {len(EXPECT)} rooms ({len(named)} case rooms, the rest the stress rooms), "
                 "the stored and active agent and the agents listed that the cases left (read through `main` at the "
                 "end of each case)",
                 proc.returncode == 0 and child.get("pid") != os.getpid() and not mism_api and bool(EXPECT),
                 observed={"child exit": proc.returncode, "mismatches": mism_api[:10],
                           "child openminion": child.get("openminion"), "sessions counted by child": child.get("sessions")})
    checks.check(f"item 10: [{label}] E reopen: read independently of openminion, the stored active_agent_id and the "
                 "active agent rows of every room match", not mism_raw, observed=mism_raw[:10])
    checks.check(f"item 4: [{label}] E: across all {len(sessions)} sessions in the database, no stored active agent is a "
                 "departed agent or a non-participant (the legacy sessions whose stored agent is the session's own "
                 "agent, as the legacy rule allows, are listed separately)",
                 not bad and len(sessions) >= len(EXPECT), observed={"violations": bad[:10], "legacy own agent": legacy})


# ======================================================================= main
def item_1(checks: Checks, info: dict[str, Any]) -> None:
    import hashlib
    import importlib.util

    cwd = Path.cwd().resolve()
    markers = [str(p) for p in [cwd, *cwd.parents] for m in (".git", "pyproject.toml") if (p / m).exists()]
    spec = importlib.util.find_spec("openminion.modules.storage.runtime.session_store.participants")
    module_file = Path(spec.origin) if spec and spec.origin else None
    CASES["installed participants.py"] = {
        "file": mask(module_file),
        "sha256": hashlib.sha256(module_file.read_bytes()).hexdigest() if module_file else None,
    }
    checks.check("item 1: the package is the installed one: imported from the venv's site-packages, Python in isolated "
                 "mode, run from an empty directory with no checkout (.git or pyproject.toml) in it or its parents",
                 info["imported_from_site_packages"] and info["isolated_mode"] and not markers
                 and not (cwd / "src" / "openminion").exists(),
                 observed={"openminion.__version__": info["openminion.__version__"],
                           "openminion.__file__": info["openminion.__file__"], "cwd": info["cwd"],
                           "checkout markers": markers, "installed participants.py": CASES["installed participants.py"]})


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
    info = banner("probe_495", backend=a.backend, rooms_per_stress=a.rooms, hold_seconds=HOLD_SECONDS)
    checks = Checks()
    item_1(checks, info)
    L = a.backend
    env = PgEnv(base, work) if a.backend == "postgres" else SqliteEnv(work)
    if env.pg:
        CASES["postgres"] = env.server()
    c = None
    try:
        c = Ctx(checks, env, L)
        for name, fn in (("A", part_a), ("T", part_t), ("I1", part_i1), ("I2", part_i2), ("I3", part_i3),
                         ("S", part_s), ("V", part_v), ("L", part_l), ("J", part_j), ("R", part_r)):
            print(f"== {L} {name}", flush=True)
            part(checks, L, name, fn, c)
        print(f"== {L} G", flush=True)
        part(checks, L, "G", part_g, c, a.rooms)
        CASES[f"{L} traced statements (all)"] = len(c.trace.entries)
    finally:
        if c is not None:
            c.close()
    print(f"== {L} E", flush=True)
    part(checks, L, "E", part_e, checks, env, L, work, a.backend)
    env.drop()
    summary = checks.summary()
    write_result(Path(a.out), {"banner": info, "summary": summary, "checks": checks.items, "expect": EXPECT,
                               "legacy_sessions": LEGACY_OWN, "cases": CASES})
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
