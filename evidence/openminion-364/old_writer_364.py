"""Issue #364: an older writer against a database upgraded to runtime migration 14.

Runs three steps, each in its own process and source tree:

1. NEW tree: migrate a fresh SQLite database (or a Postgres schema), create a
   room and join `a` and `b` on one tick.
2. OLD tree (a checkout from before the change): on the same tick, add `old-1`,
   then `old-2`, then remove `a` and rejoin it.
3. NEW tree: list the room with its stored join_seq values.

Usage, from anywhere:
  python old_writer_364.py <new-src> <old-src> [postgres]
With `postgres`, OPENMINION_TEST_POSTGRES_URL must be set (postgres extra).
"""

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote_plus

STEP = r'''
import json, sys
from unittest.mock import patch
from openminion.modules.storage.runtime.context import build_runtime_storage

step, db, url = sys.argv[1], sys.argv[2], sys.argv[3]
CLOCK = "openminion.modules.storage.runtime.session_store.participants.utc_now_iso"
TIED = "2026-10-22T01:00:00+00:00"
kwargs = {"env": {}}
if url:
    kwargs.update(record_backend="record.postgres", record_backend_options={"url": url})
storage = build_runtime_storage(db, **kwargs)
sessions = storage.sessions
out = {"step": step}
try:
    with patch(CLOCK, return_value=TIED):
        if step == "new-setup":
            room = sessions.create_room(channel="cli", target="mixed", session_id="room-mixed")
            for agent in ("a", "b"):
                sessions.add_participant(session_id=room.id, participant_type="agent", participant_id=agent)
        elif step == "old-writes":
            for agent in ("old-1", "old-2"):
                try:
                    sessions.add_participant(session_id="room-mixed", participant_type="agent", participant_id=agent)
                    out[agent] = "added"
                except Exception as exc:
                    out[agent] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
            sessions.remove_participant(session_id="room-mixed", participant_type="agent", participant_id="a")
            sessions.add_participant(session_id="room-mixed", participant_type="agent", participant_id="a")
            out["a"] = "rejoined"
        out["migration"] = storage.migration_result.current_version
        out["listed"] = [p.participant_id for p in sessions.list_participants("room-mixed")]
        if step == "new-list":
            rows = sessions._backend.query_dicts(
                "SELECT participant_id, join_seq FROM room_participants WHERE session_id = ?",
                ("room-mixed",),
            )
            out["join_seq"] = {r["participant_id"]: int(r["join_seq"]) for r in rows}
finally:
    storage.close()
print(json.dumps(out))
'''


def run(src, step, db, url):
    env = dict(os.environ, PYTHONPATH=src)
    done = subprocess.run(
        [sys.executable, "-c", STEP, step, db, url],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    print(done.stdout.strip() or done.stderr.strip().splitlines()[-1])


def main():
    new_src, old_src = sys.argv[1], sys.argv[2]
    postgres = len(sys.argv) > 3 and sys.argv[3] == "postgres"
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "state" / "openminion.db")
        url = ""
        admin = None
        if postgres:
            import sqlalchemy as sa

            base = os.environ["OPENMINION_TEST_POSTGRES_URL"].strip()
            schema = f"old364_{uuid.uuid4().hex}"
            admin = sa.create_engine(base, future=True)
            with admin.begin() as conn:
                conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            sep = "&" if "?" in base else "?"
            url = f"{base}{sep}options={quote_plus(f'-csearch_path={schema}')}"
        try:
            run(new_src, "new-setup", db, url)
            run(old_src, "old-writes", db, url)
            run(new_src, "new-list", db, url)
        finally:
            if admin is not None:
                with admin.begin() as conn:
                    conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                admin.dispose()


if __name__ == "__main__":
    main()
