"""Issue #364 probe: room participants that join on one clock tick.

The issue's reproduction, with the participant clock patched to one timestamp
and the participant row ids patched to "f" * 32, "c" * 32, "0" * 32 (each id
sorts before the previous one):

- agents `first`, `second`, `third` join a room in that order;
- `list_participants` order, then removing the active `first`, and the next
  active agent;
- then the upsert contract: an already-active participant that is added again
  keeps its place and `joined_at`, and a participant that leaves and rejoins
  in the same tick moves to the end (renewed place).

SQLite always; PostgreSQL too when OPENMINION_TEST_POSTGRES_URL is set (needs
the postgres extra). Run from an openminion checkout:
PYTHONPATH=src python probe_364.py   (on Windows: set PYTHONPATH=src)
The second room uses ids "e", "b", "7" (row ids are unique across rooms).
"""

import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote_plus

from openminion.modules.storage.runtime.context import build_runtime_storage

CLOCK = "openminion.modules.storage.runtime.session_store.participants.utc_now_iso"
UUID = "openminion.modules.storage.runtime.session_store.participants.uuid4"
TIED_AT = "2026-10-22T01:00:00+00:00"
LATER = "2026-10-22T01:00:05+00:00"


def ids(*chars):
    return [SimpleNamespace(hex=char * 32) for char in chars]


def names(sessions, room_id):
    return [item.participant_id for item in sessions.list_participants(room_id)]


def run(sessions):
    room = sessions.create_room(channel="cli", target="review")
    with patch(CLOCK, return_value=TIED_AT), patch(UUID, side_effect=ids("f", "c", "0")):
        for agent in ("first", "second", "third"):
            sessions.add_participant(
                session_id=room.id, participant_type="agent", participant_id=agent
            )
    listed = sessions.list_participants(room.id)
    out = {
        "stored": [
            [item.participant_id, item.id[:1] * 3, item.joined_at] for item in listed
        ],
        "list_after_joins": names(sessions, room.id),
        "active_after_joins": sessions.get_active_agent(room.id),
    }
    with patch(CLOCK, return_value=TIED_AT):
        sessions.remove_participant(
            session_id=room.id, participant_type="agent", participant_id="first"
        )
    out["active_after_removing_first"] = sessions.get_active_agent(room.id)
    out["list_after_removing_first"] = names(sessions, room.id)

    # Upsert contract on a second room: keep the place of an already-active
    # participant, renew it after leave and rejoin.
    room2 = sessions.create_room(channel="cli", target="rejoin")
    with patch(CLOCK, return_value=TIED_AT), patch(
        UUID, side_effect=ids("e", "b", "7", "6", "5")
    ):
        for agent in ("first", "second", "third"):
            sessions.add_participant(
                session_id=room2.id, participant_type="agent", participant_id=agent
            )
        with patch(CLOCK, return_value=LATER):
            again = sessions.add_participant(
                session_id=room2.id,
                participant_type="agent",
                participant_id="first",
                display_name="First again",
            )
        out["upsert_active_first_joined_at"] = again.joined_at
        out["list_after_active_upsert"] = names(sessions, room2.id)
        sessions.remove_participant(
            session_id=room2.id, participant_type="agent", participant_id="first"
        )
        sessions.add_participant(
            session_id=room2.id, participant_type="agent", participant_id="first"
        )
    out["list_after_same_tick_rejoin"] = names(sessions, room2.id)
    return out


@contextmanager
def postgres_url():
    import sqlalchemy as sa

    base = os.environ["OPENMINION_TEST_POSTGRES_URL"].strip()
    schema = f"probe364_{uuid.uuid4().hex}"
    admin = sa.create_engine(base, future=True)
    with admin.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    sep = "&" if "?" in base else "?"
    try:
        yield f"{base}{sep}options={quote_plus(f'-csearch_path={schema}')}"
    finally:
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        storage = build_runtime_storage(Path(tmp) / "state" / "openminion.db", env={})
        try:
            print(json.dumps({"backend": "sqlite", **run(storage.sessions)}))
        finally:
            storage.close()
        if os.getenv("OPENMINION_TEST_POSTGRES_URL", "").strip():
            with postgres_url() as url:
                storage = build_runtime_storage(
                    Path(tmp) / "pg" / "openminion.db",
                    env={},
                    record_backend="record.postgres",
                    record_backend_options={"url": url},
                )
                try:
                    print(json.dumps({"backend": "postgres", **run(storage.sessions)}))
                finally:
                    storage.close()


if __name__ == "__main__":
    main()
