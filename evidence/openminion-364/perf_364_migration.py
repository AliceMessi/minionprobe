"""Issue #364: time runtime migration 14 on a database with many participants.

Builds a database at runtime migration 13 with N sessions of 2 participants
each (one joined_at per session, so every session has a tie) plus one room of
R participants on one joined_at, then times migration 14.

Before the upgrade it reads an independent numbering (each row counts the rows
of its session at or before it in (joined_at, id) order, the query the tests
use) and, after it, reports how many stored join_seq values differ from it.
That count is quadratic in the size of a session; pass 0 as the third
argument to skip it (timing only).

SQLite always; PostgreSQL too when OPENMINION_TEST_POSTGRES_URL is set.
Run from an openminion checkout:
PYTHONPATH=src python perf_364_migration.py [N] [R] [oracle 1|0]
"""

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote_plus

from openminion.modules.storage.runtime.migrations import (
    DEFAULT_MIGRATIONS,
    migrate_database,
    migrate_record_store,
)

N = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
R = int(sys.argv[2]) if len(sys.argv) > 2 else 1_000
ORACLE = (sys.argv[3] if len(sys.argv) > 3 else "1") == "1"
BEFORE = tuple(item for item in DEFAULT_MIGRATIONS if item.version < 14)
TIED = "2026-10-22T01:00:00+00:00"
COUNTED = """
SELECT participant.id AS id, (
    SELECT COUNT(*)
    FROM room_participants AS earlier
    WHERE earlier.session_id = participant.session_id
      AND (earlier.joined_at, earlier.id) <= (participant.joined_at, participant.id)
) AS join_seq
FROM room_participants AS participant
"""
STORED = "SELECT id, join_seq FROM room_participants"


def rows():
    sessions = [(f"s{i}", f"key-s{i}", "cli", f"s{i}") for i in range(N)]
    sessions.append(("big-room", "room:big-room", "cli", "big-room"))
    participants = []
    for i in range(N):
        for j in range(2):
            participants.append((uuid.uuid4().hex, f"s{i}", "agent", f"a{j}", TIED))
    for j in range(R):
        participants.append((uuid.uuid4().hex, "big-room", "agent", f"m{j}", TIED))
    return sessions, participants


def differing(counted, stored):
    if counted is None:
        return "not checked"
    return str(sum(1 for key, value in stored.items() if counted.get(key) != value))


def report(backend, participants, result, elapsed, counted, stored):
    print(
        f"{backend}: {len(participants)} participants in {N + 1} sessions "
        f"(one room of {R}), migration {result.applied_versions} took "
        f"{elapsed:.2f} s, join_seq values differing from the counted numbering: "
        f"{differing(counted, stored)} of {len(stored)}"
    )


def sqlite_case(tmp):
    import sqlite3

    path = Path(tmp) / "perf.db"
    migrate_database(path, migrations=BEFORE)
    sessions, participants = rows()
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO sessions(id, session_key, channel, target) VALUES (?, ?, ?, ?)",
        sessions,
    )
    conn.executemany(
        "INSERT INTO room_participants(id, session_id, participant_type, "
        "participant_id, joined_at) VALUES (?, ?, ?, ?, ?)",
        participants,
    )
    conn.commit()
    counted = dict(conn.execute(COUNTED).fetchall()) if ORACLE else None
    conn.close()
    start = time.perf_counter()
    result = migrate_database(path)
    elapsed = time.perf_counter() - start
    conn = sqlite3.connect(path)
    stored = dict(conn.execute(STORED).fetchall())
    conn.close()
    report(f"sqlite {sqlite3.sqlite_version}", participants, result, elapsed, counted, stored)


def postgres_case():
    import sqlalchemy as sa

    from openminion.modules.storage.backends.postgres import RecordStorePostgres

    base = os.environ["OPENMINION_TEST_POSTGRES_URL"].strip()
    schema = f"perf364_{uuid.uuid4().hex}"
    admin = sa.create_engine(base, future=True)
    with admin.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    sep = "&" if "?" in base else "?"
    store = RecordStorePostgres(
        f"{base}{sep}options={quote_plus(f'-csearch_path={schema}')}"
    )
    try:
        migrate_record_store(store, backend_type="postgres", migrations=BEFORE)
        sessions, participants = rows()
        keys = ("id", "session_key", "channel", "target")
        store.insert_many("sessions", [dict(zip(keys, row)) for row in sessions])
        keys = ("id", "session_id", "participant_type", "participant_id", "joined_at")
        for chunk in range(0, len(participants), 20_000):
            store.insert_many(
                "room_participants",
                [dict(zip(keys, row)) for row in participants[chunk : chunk + 20_000]],
            )
        store.execute_count("ANALYZE room_participants")
        version = store.query_dicts("SHOW server_version")[0]["server_version"]
        counted = (
            {row["id"]: int(row["join_seq"]) for row in store.query_dicts(COUNTED)}
            if ORACLE
            else None
        )
        start = time.perf_counter()
        result = migrate_record_store(store, backend_type="postgres")
        elapsed = time.perf_counter() - start
        stored = {row["id"]: int(row["join_seq"]) for row in store.query_dicts(STORED)}
        report(f"postgres {version}", participants, result, elapsed, counted, stored)
    finally:
        store.close()
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        sqlite_case(tmp)
    if os.getenv("OPENMINION_TEST_POSTGRES_URL", "").strip():
        postgres_case()


if __name__ == "__main__":
    main()
