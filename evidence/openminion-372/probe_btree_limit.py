"""#372: does a plain B-tree index on the encoded run key limit run ids on Postgres?

Inserts (session_id, JSON-encoded run id, event_id) rows into a temporary table
with a B-tree index on those columns, as a direct index on the key would be,
for run ids of growing length. Random ids, so compression does not hide the
size. A non-ASCII id is escaped by `json.dumps`, six bytes per character.

    python probe_btree_limit.py   # needs OPENMINION_TEST_POSTGRES_URL
"""

import json
import os
import random

import sqlalchemy
from sqlalchemy import text

rng = random.Random(372)
alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
engine = sqlalchemy.create_engine(os.environ["OPENMINION_TEST_POSTGRES_URL"])
with engine.connect() as conn:
    print("server", conn.execute(text("SHOW server_version")).scalar())
    conn.execute(text("CREATE TEMP TABLE k (session_id text, run_key text, event_id bigint)"))
    conn.execute(text("CREATE INDEX k_run ON k (session_id, run_key, event_id)"))
    conn.commit()
    cases = [("ascii", n) for n in (1000, 2000, 2600, 2700, 3000, 10000)]
    cases += [("cjk", n) for n in (300, 450, 500, 2000)]
    for event_id, (kind, length) in enumerate(cases, 1):
        if kind == "ascii":
            run_id = "".join(rng.choice(alphabet) for _ in range(length))
        else:
            run_id = "".join(chr(0x4E00 + rng.randrange(20000)) for _ in range(length))
        key = json.dumps(run_id)
        try:
            conn.execute(
                text("INSERT INTO k VALUES ('session-1', :key, :event_id)"),
                {"key": key, "event_id": event_id},
            )
            conn.commit()
            outcome = "accepted"
        except Exception as exc:  # noqa: BLE001 - the error is the observation
            conn.rollback()
            outcome = str(getattr(exc, "orig", exc)).splitlines()[0][:110]
        print(f"{kind} run id of {length} chars, key {len(key)} bytes: {outcome}")
