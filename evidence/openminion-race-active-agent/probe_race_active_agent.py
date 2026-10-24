"""Room active agent: the check and the write in add_participant on two connections.

`add_participant` commits the participant row, then reads the session, and when
it finds no active agent writes `active_agent_id` with an unconditional UPDATE
in a separate statement. This probe opens two runtime storages ("connections")
on one database and pauses one of them between that read and that write, then
drives the other connection:

1. two first joins: `alpha` joins on A, `beta` joins on B, both read no active
   agent, A writes, then B writes;
2. explicit selection: `alpha` joins on A and reads no active agent; on B,
   `beta` joins and is explicitly selected with `set_active_agent`; then A
   writes;
3. leave: `alpha` joins on A and reads no active agent; on B, `alpha` leaves;
   then A writes.

The pause only delays the paused thread: the patched session read returns the
real record. The participant clock is patched to return strictly increasing
timestamps, so the listing order is the join order (on `dev` participants that
join on one timestamp list in row id order, #364). Each room first gets a human
owner, as `openminion room create` does. A fourth part runs both first joins
concurrently with no pause and counts the rooms where both joins read no active
agent (so both wrote one) and where the stored active agent is then not the
first agent listed.

SQLite always; PostgreSQL too when OPENMINION_TEST_POSTGRES_URL is set (needs
the postgres extra). Run from an openminion source checkout:
PYTHONPATH=src python probe_race_active_agent.py [rooms for part 4, default 200]
(on Windows: set PYTHONPATH=src)
"""

import itertools
import os
import sqlite3
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote_plus

from openminion.modules.storage.runtime.context import build_runtime_storage

ROOMS = int(sys.argv[1]) if len(sys.argv) > 1 else 200
TIMEOUT = 20
CLOCK = "openminion.modules.storage.runtime.session_store.participants.utc_now_iso"
_TICKS = itertools.count(1)
_TICK_LOCK = threading.Lock()


def increasing_clock():
    with _TICK_LOCK:
        tick = next(_TICKS)
    start = datetime(2026, 10, 24, tzinfo=timezone.utc)
    return (start + timedelta(milliseconds=tick)).isoformat()


class Gate:
    """What one add_participant read right after its join, and its pause."""

    def __init__(self, pause):
        self.pause = pause
        self.checked = threading.Event()
        self.go = threading.Event()
        self.seen = []


def instrument(storage):
    """Wrap the storage's participant owner once.

    Records what add_participant reads from the session right after its join
    (the read that decides whether it writes the active agent) in the armed
    gate and, when the gate pauses, waits there until the gate's `go` is set.
    """
    owner = storage.sessions._participants
    real_get = owner._get_session
    real_upsert = owner.upsert_participant_record
    joined = threading.local()
    storage.gate = None

    def upsert(**kwargs):
        real_upsert(**kwargs)
        joined.flag = True

    def get_session(session_id):
        record = real_get(session_id)
        gate = storage.gate
        if getattr(joined, "flag", False):
            joined.flag = False
            if gate is not None:
                gate.seen.append(record.active_agent_id)
                gate.checked.set()
                if gate.pause and not gate.go.wait(TIMEOUT):
                    raise TimeoutError("paused join was never released")
        return record

    owner.upsert_participant_record = upsert
    owner._get_session = get_session
    return storage


def arm(storage, *, pause):
    storage.gate = Gate(pause)
    return storage.gate


def disarm(*storages):
    for storage in storages:
        storage.gate = None


def start(target):
    box = {}

    def run():
        try:
            box["result"] = target()
        except BaseException as exc:  # reported, not raised
            box["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def finish(thread, box):
    thread.join(TIMEOUT)
    if thread.is_alive():
        return "still running (blocked)"
    return box.get("error", "ok")


def join(storage, room_id, agent):
    return storage.sessions.add_participant(
        session_id=room_id, participant_type="agent", participant_id=agent
    )


def new_room(main, name):
    room = main.sessions.create_room(channel="cli", target=name)
    main.sessions.add_participant(
        session_id=room.id, participant_type="human", participant_id="owner", role="owner"
    )
    return room.id


def state(main, room_id):
    sessions = main.sessions
    agents = [
        item.participant_id
        for item in sessions.list_participants(room_id)
        if item.participant_type == "agent"
    ]
    return (
        f"agents listed: {agents} | stored active_agent_id: "
        f"{sessions.get_session(room_id).active_agent_id!r} | "
        f"get_active_agent: {sessions.get_active_agent(room_id)!r}"
    )


def two_first_joins(main, a, b):
    room_id = new_room(main, "two-first-joins")
    gate_a, gate_b = arm(a, pause=True), arm(b, pause=True)
    thread_a = start(lambda: join(a, room_id, "alpha"))
    assert gate_a.checked.wait(TIMEOUT)
    thread_b = start(lambda: join(b, room_id, "beta"))
    assert gate_b.checked.wait(TIMEOUT)
    print(f"  A (alpha) read active_agent_id={gate_a.seen[0]!r}; B (beta) read {gate_b.seen[0]!r}")
    gate_a.go.set()
    print(f"  A's add_participant(alpha): {finish(*thread_a)}")
    print(f"  after A returns: {state(main, room_id)}")
    gate_b.go.set()
    print(f"  B's add_participant(beta): {finish(*thread_b)}")
    print(f"  after B returns: {state(main, room_id)}")
    disarm(a, b)


def explicit_selection(main, a, b):
    room_id = new_room(main, "explicit-selection")
    gate_a = arm(a, pause=True)
    thread_a = start(lambda: join(a, room_id, "alpha"))
    assert gate_a.checked.wait(TIMEOUT)
    print(f"  A (alpha) read active_agent_id={gate_a.seen[0]!r}")
    disarm(b)
    join(b, room_id, "beta")
    selected = b.sessions.set_active_agent(session_id=room_id, agent_id="beta")
    print(f"  B: add_participant(beta), then set_active_agent(beta) -> active_agent_id={selected.active_agent_id!r}")
    print(f"  after B's explicit selection: {state(main, room_id)}")
    gate_a.go.set()
    print(f"  A's add_participant(alpha): {finish(*thread_a)}")
    print(f"  after A returns: {state(main, room_id)}")
    disarm(a, b)


def leave(main, a, b):
    room_id = new_room(main, "leave")
    gate_a = arm(a, pause=True)
    thread_a = start(lambda: join(a, room_id, "alpha"))
    assert gate_a.checked.wait(TIMEOUT)
    print(f"  A (alpha) read active_agent_id={gate_a.seen[0]!r}")
    disarm(b)
    removed = b.sessions.remove_participant(
        session_id=room_id, participant_type="agent", participant_id="alpha"
    )
    print(f"  B: remove_participant(alpha) -> {removed}")
    print(f"  after B's leave: {state(main, room_id)}")
    gate_a.go.set()
    print(f"  A's add_participant(alpha): {finish(*thread_a)}")
    print(f"  after A returns: {state(main, room_id)}")
    try:
        main.sessions.set_active_agent(session_id=room_id, agent_id="alpha")
        print("  set_active_agent(alpha) now: accepted")
    except ValueError as exc:
        print(f"  set_active_agent(alpha) now: ValueError: {exc}")
    disarm(a, b)


def unpaused(main, a, b):
    both_wrote = not_first_listed = errors = 0
    for index in range(ROOMS):
        room_id = new_room(main, f"unpaused-{index}")
        gate_a, gate_b = arm(a, pause=False), arm(b, pause=False)
        barrier = threading.Barrier(2)

        def go(storage, agent):
            barrier.wait(TIMEOUT)
            return join(storage, room_id, agent)

        threads = [start(lambda: go(a, "alpha")), start(lambda: go(b, "beta"))]
        if any(finish(*item) != "ok" for item in threads):
            errors += 1
            continue
        if not gate_a.seen[0] and not gate_b.seen[0]:
            both_wrote += 1
            agents = [
                item.participant_id
                for item in main.sessions.list_participants(room_id)
                if item.participant_type == "agent"
            ]
            if main.sessions.get_session(room_id).active_agent_id != agents[0]:
                not_first_listed += 1
    disarm(a, b)
    print(
        f"  {ROOMS} rooms: both joins read no active agent and wrote one in {both_wrote}; "
        f"in {not_first_listed} of those the stored active agent is not the first agent listed; "
        f"errors: {errors}"
    )


def run_backend(label, open_storage):
    print(f"== {label}")
    main = open_storage()
    a, b = instrument(open_storage()), instrument(open_storage())
    try:
        with patch(CLOCK, increasing_clock):
            for title, part in (
                ("1. two first joins", two_first_joins),
                ("2. explicit selection during a first join", explicit_selection),
                ("3. leave during a first join", leave),
                ("4. two first joins, no pause", unpaused),
            ):
                print(title)
                part(main, a, b)
    finally:
        for storage in (main, a, b):
            storage.close()


@contextmanager
def postgres_url():
    import sqlalchemy as sa

    base = os.environ["OPENMINION_TEST_POSTGRES_URL"].strip()
    schema = f"probe_race_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(base, future=True)
    with admin.begin() as conn:
        version = conn.execute(sa.text("SHOW server_version")).scalar()
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    sep = "&" if "?" in base else "?"
    try:
        yield f"{base}{sep}options={quote_plus(f'-csearch_path={schema}')}", version
    finally:
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def main():
    print(f"python {sys.version.split()[0]} {sys.platform}")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / "state" / "openminion.db"
        run_backend(
            f"SQLite {sqlite3.sqlite_version}",
            lambda: build_runtime_storage(path, env={}),
        )
        if os.getenv("OPENMINION_TEST_POSTGRES_URL", "").strip():
            with postgres_url() as (url, version):
                run_backend(
                    f"PostgreSQL {version}",
                    lambda: build_runtime_storage(
                        Path(tmp) / "pg" / f"{uuid.uuid4().hex}.db",
                        env={},
                        record_backend="record.postgres",
                        record_backend_options={"url": url},
                    ),
                )
        else:
            print("== PostgreSQL: skipped (OPENMINION_TEST_POSTGRES_URL not set)")


if __name__ == "__main__":
    main()
