"""Run-status reader timings (#352) on SQLite or Postgres, for any source tree.

python perf352.py sqlite|postgres [sizes...]

Per size: a session of N events in turns of 5 (run.queued with an invocation,
run.running, tool.call, run.completed, response.delivered; ~400-byte payloads,
uuid run ids, one thread) plus a second session of N events. Median of 5 calls
(3 for limit=500) per reader, after one warm-up call.
"""

import json
import os
import statistics
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import openminion
from openminion.modules.storage.runtime.context import build_runtime_storage
from openminion.modules.task.run import (
    list_session_run_events,
    list_session_runs,
    resolve_thread_lifecycle,
)


def rows_for(session_id, size, base):
    rows, runs = [], []
    for index in range(size):
        turn, slot = divmod(index, 5)
        if slot == 0:
            runs.append(uuid.uuid4().hex)
        event_type = ("run.queued", "run.running", "tool.call", "run.completed",
                      "response.delivered")[slot]
        payload = {"run_id": runs[-1], "state": event_type.split(".")[1], "step": "step",
                   "conversation_id": "conv", "thread_id": "thread",
                   "request_id": f"req-{turn}", "pad": "x" * 260}
        if slot == 0:
            payload["invocation_id"] = f"inv-{turn}"
        rows.append({"session_id": session_id, "event_type": event_type,
                     "payload_json": json.dumps(payload, sort_keys=True),
                     "created_at": (base + timedelta(milliseconds=index)).isoformat()})
    return rows, runs


def timed(fn, reps=5):
    fn()
    samples = []
    for _ in range(reps):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def main():
    backend = sys.argv[1]
    sizes = [int(s) for s in sys.argv[2:]] or [2_000, 10_000, 50_000]
    print("openminion from", Path(openminion.__file__).parent.parent)
    for size in sizes:
        tmp = Path(tempfile.mkdtemp())
        options = {}
        if backend == "postgres":
            import sqlalchemy

            url = os.environ["OPENMINION_TEST_POSTGRES_URL"]
            schema = f"perf352_{uuid.uuid4().hex[:8]}"
            admin = sqlalchemy.create_engine(url, future=True)
            with admin.begin() as conn:
                conn.execute(sqlalchemy.text(f'CREATE SCHEMA "{schema}"'))
            from urllib.parse import quote_plus

            options = {"record_backend": "record.postgres", "record_backend_options": {
                "url": f"{url}?options={quote_plus(f'-csearch_path={schema}')}"}}
        storage = build_runtime_storage(tmp / "state" / "openminion.db", env={}, **options)
        try:
            sessions = storage.sessions
            target = sessions.resolve_session(agent_id="main", channel="console", target="t")
            other = sessions.resolve_session(agent_id="main", channel="console", target="o")
            base = datetime(2026, 10, 23, tzinfo=timezone.utc)
            rows, runs = rows_for(target.id, size, base)
            other_rows, _ = rows_for(other.id, size, base)
            merged = [row for pair in zip(rows, other_rows) for row in pair]
            if backend == "postgres":
                for start in range(0, len(merged), 5000):
                    storage.record_store.insert_many("events", merged[start:start + 5000])
                storage.record_store.execute_count("ANALYZE events")
            else:
                conn = storage.connection
                conn.executemany(
                    "INSERT INTO events(session_id, event_type, payload_json, created_at) "
                    "VALUES (:session_id, :event_type, :payload_json, :created_at)", merged)
                conn.commit()
            out = {
                "runs(limit=20)": timed(lambda: list_session_runs(sessions, session_id=target.id)),
                "runs(limit=500)": timed(lambda: list_session_runs(
                    sessions, session_id=target.id, limit=500), reps=3),
                "run_events(newest)": timed(lambda: list_session_run_events(
                    sessions, session_id=target.id, run_id=runs[-1])),
                "run_events(oldest)": timed(lambda: list_session_run_events(
                    sessions, session_id=target.id, run_id=runs[0])),
                "thread_lifecycle": timed(lambda: resolve_thread_lifecycle(
                    sessions, session_id=target.id, conversation_id="conv",
                    thread_id="thread")),
            }
            print(f"{backend} {size:>6} events/session: "
                  + "; ".join(f"{k} {v:.1f} ms" for k, v in out.items()), flush=True)
        finally:
            storage.close()
            if backend == "postgres":
                with admin.begin() as conn:
                    conn.execute(sqlalchemy.text(f'DROP SCHEMA "{schema}" CASCADE'))
                admin.dispose()


if __name__ == "__main__":
    main()
