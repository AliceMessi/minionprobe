"""Room active agent (#393): two first joins with no pause, counted on any tree.

The report's probe (`probe_race_active_agent.py`) records the first session read
after a join's participant write returns. With the fix the activation is read
and written inside that write's transaction, so that read comes after the
commit and the report's part 4 counts 0 by construction. This probe counts the
same race without a pause and without depending on where the read is:

- the statements each join runs that write the room's active agent
  (`UPDATE sessions SET active_agent_id ...`, the activation write on both
  trees), so "both joins wrote the active agent" is measured directly;
- the active agent each join's caller reads right after its own
  `add_participant` returns, against the stored one after both joins: a caller
  that saw an active agent that was then replaced without a selection or leave.

Two runtime storages on one database (the same SQLite file, or the same
PostgreSQL schema when OPENMINION_TEST_POSTGRES_URL is set) join `alpha` and
`beta` to a fresh room from a barrier, each in its own thread, in 200 rooms by
default. Each room first gets a human owner, as `openminion room create` does.

Run from an openminion source checkout:
PYTHONPATH=src python probe_393_unpaused.py [rooms, default 200]
"""

import os
import sqlite3
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote_plus

from openminion.modules.storage.runtime.context import build_runtime_storage

ROOMS = int(sys.argv[1]) if len(sys.argv) > 1 else 200
TIMEOUT = 30
ACTIVATION_WRITE = "UPDATE sessions SET active_agent_id"


def count_activation_writes(storage):
    """Count this storage's active-agent writes in `storage.activation_writes`."""
    backend = storage.sessions._backend
    execute_count = backend.execute_count
    storage.activation_writes = 0

    def counted(sql, params=None):
        if sql.lstrip().startswith(ACTIVATION_WRITE):
            storage.activation_writes += 1
        return execute_count(sql, params)

    backend.execute_count = counted
    return storage


def new_room(main, name):
    room = main.sessions.create_room(channel="cli", target=name)
    main.sessions.add_participant(
        session_id=room.id, participant_type="human", participant_id="owner", role="owner"
    )
    return room.id


def run_backend(label, open_storage):
    print(f"== {label}")
    main = open_storage()
    a, b = count_activation_writes(open_storage()), count_activation_writes(open_storage())
    both_wrote = seen_replaced = errors = 0
    try:
        for index in range(ROOMS):
            room_id = new_room(main, f"unpaused-{index}")
            barrier = threading.Barrier(2)
            seen, failed = {}, []

            def join(storage, agent):
                try:
                    barrier.wait(TIMEOUT)
                    storage.sessions.add_participant(
                        session_id=room_id, participant_type="agent", participant_id=agent
                    )
                    seen[agent] = storage.sessions.get_active_agent(room_id)
                except BaseException as exc:  # counted, not raised
                    failed.append(f"{type(exc).__name__}: {exc}")

            a.activation_writes = b.activation_writes = 0
            threads = [
                threading.Thread(target=join, args=(a, "alpha"), daemon=True),
                threading.Thread(target=join, args=(b, "beta"), daemon=True),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(TIMEOUT)
            if failed or any(thread.is_alive() for thread in threads):
                errors += 1
                continue
            if a.activation_writes and b.activation_writes:
                both_wrote += 1
            stored = main.sessions.get_session(room_id).active_agent_id
            if any(value != stored for value in seen.values()):
                seen_replaced += 1
    finally:
        for storage in (main, a, b):
            storage.close()
    print(
        f"  {ROOMS} rooms: both joins wrote the active agent in {both_wrote}; "
        f"a join's caller saw an active agent that was then replaced in {seen_replaced}; "
        f"errors: {errors}"
    )


@contextmanager
def postgres_url():
    import sqlalchemy as sa

    base = os.environ["OPENMINION_TEST_POSTGRES_URL"].strip()
    schema = f"probe_393_{uuid.uuid4().hex[:12]}"
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
