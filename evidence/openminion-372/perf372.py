"""Run-status costs (#352 / #372) on SQLite or Postgres, for any source tree.

    python perf372.py sqlite|postgres [sizes...]

Per size N, two sessions of N events each, in turns of 5 (run.queued with an
invocation, run.running, tool.call, run.completed, response.delivered;
~400-byte payloads, uuid run ids, one thread), inserted in bulk.

- trees with runtime migration 13: the events are inserted at migration 12,
  then the migration runs; "backfill" is its time for the 2N events, and
  "facts" the storage it adds (SQLite pages, Postgres relation sizes).
- readers: median of 5 calls (3 for limit=500) after one warm-up. On trees
  with the facts index on run keys, the readers are timed again without it.
- write: `append_event` of 1,000 run.running events (same payload shape) to a
  third session after the bulk history; mean and median microseconds.
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
from urllib.parse import quote_plus

import openminion
from openminion.modules.storage.runtime import migrations as runtime_migrations
from openminion.modules.storage.runtime.context import build_runtime_storage
from openminion.modules.task.run import (
    list_session_run_events,
    list_session_runs,
    resolve_thread_lifecycle,
)

HAS_FACTS = any(m.version == 13 for m in runtime_migrations.DEFAULT_MIGRATIONS)
BEFORE_FACTS = tuple(m for m in runtime_migrations.DEFAULT_MIGRATIONS if m.version < 13)
EVENT_TYPES = (
    "run.queued",
    "run.running",
    "tool.call",
    "run.completed",
    "response.delivered",
)


def payload_for(run_id, slot, turn):
    payload = {
        "run_id": run_id,
        "state": EVENT_TYPES[slot].split(".")[1],
        "step": "step",
        "conversation_id": "conv",
        "thread_id": "thread",
        "request_id": f"req-{turn}",
        "pad": "x" * 260,
    }
    if slot == 0:
        payload["invocation_id"] = f"inv-{turn}"
    return payload


def rows_for(session_id, size, base):
    rows, runs = [], []
    for index in range(size):
        turn, slot = divmod(index, 5)
        if slot == 0:
            runs.append(uuid.uuid4().hex)
        rows.append(
            {
                "session_id": session_id,
                "event_type": EVENT_TYPES[slot],
                "payload_json": json.dumps(payload_for(runs[-1], slot, turn), sort_keys=True),
                "created_at": (base + timedelta(milliseconds=index)).isoformat(),
            }
        )
    return rows, runs


def timed(fn, reps=5):
    fn()
    samples = []
    for _ in range(reps):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def readers(sessions, target, runs):
    return {
        "runs20": timed(lambda: list_session_runs(sessions, session_id=target)),
        "runs500": timed(
            lambda: list_session_runs(sessions, session_id=target, limit=500), reps=3
        ),
        "events_newest": timed(
            lambda: list_session_run_events(sessions, session_id=target, run_id=runs[-1])
        ),
        "events_oldest": timed(
            lambda: list_session_run_events(sessions, session_id=target, run_id=runs[0])
        ),
        "thread": timed(
            lambda: resolve_thread_lifecycle(
                sessions, session_id=target, conversation_id="conv", thread_id="thread"
            )
        ),
    }


def fmt(values):
    return "; ".join(f"{k} {v:.1f}" for k, v in values.items())


def main():
    backend = sys.argv[1]
    sizes = [int(s) for s in sys.argv[2:]] or [2_000, 10_000, 50_000]
    print("openminion from", Path(openminion.__file__).parent.parent, "facts:", HAS_FACTS)
    base = datetime(2026, 10, 23, tzinfo=timezone.utc)
    for size in sizes:
        tmp = Path(tempfile.mkdtemp())
        db_path = tmp / "state" / "openminion.db"
        options = {}
        admin = schema = None
        record_store = None
        if backend == "postgres":
            import sqlalchemy

            from openminion.modules.storage.backends.postgres import RecordStorePostgres

            url = os.environ["OPENMINION_TEST_POSTGRES_URL"]
            schema = f"perf372_{uuid.uuid4().hex[:8]}"
            admin = sqlalchemy.create_engine(url, future=True)
            with admin.begin() as conn:
                conn.execute(sqlalchemy.text(f'CREATE SCHEMA "{schema}"'))
            schema_url = f"{url}?options={quote_plus(f'-csearch_path={schema}')}"
            options = {
                "record_backend": "record.postgres",
                "record_backend_options": {"url": schema_url},
            }
            record_store = RecordStorePostgres(schema_url)
            runtime_migrations.migrate_record_store(
                record_store, backend_type="postgres", migrations=BEFORE_FACTS
            )
        else:
            runtime_migrations.migrate_database(db_path, migrations=BEFORE_FACTS)
        line = {}
        try:
            target, other, writer = "perf-target", "perf-other", "perf-writer"
            session_rows = [
                {"id": sid, "session_key": f"key-{sid}", "channel": "console", "target": sid}
                for sid in (target, other, writer)
            ]
            rows, runs = rows_for(target, size, base)
            other_rows, _ = rows_for(other, size, base)
            merged = [row for pair in zip(rows, other_rows) for row in pair]
            if backend == "postgres":
                record_store.insert_many("sessions", session_rows)
                for start in range(0, len(merged), 5000):
                    record_store.insert_many("events", merged[start : start + 5000])
                record_store.execute_count("ANALYZE events")
                size_sql = (
                    "SELECT pg_total_relation_size('events') AS events, "
                    "COALESCE(pg_total_relation_size(to_regclass('event_run_facts')), 0)"
                    " AS facts"
                )
                if HAS_FACTS:
                    start = time.perf_counter()
                    runtime_migrations.migrate_record_store(record_store, backend_type="postgres")
                    line["backfill_ms"] = (time.perf_counter() - start) * 1000
                    record_store.execute_count("ANALYZE event_run_facts")
                    sizes_row = record_store.query_dicts(size_sql)[0]
                    line["events_kb"] = sizes_row["events"] / 1024
                    line["facts_kb"] = sizes_row["facts"] / 1024
                record_store.close()
            else:
                import sqlite3

                conn = sqlite3.connect(db_path)
                conn.executemany(
                    "INSERT INTO sessions(id, session_key, channel, target) "
                    "VALUES (:id, :session_key, :channel, :target)",
                    session_rows,
                )
                conn.executemany(
                    "INSERT INTO events(session_id, event_type, payload_json, created_at) "
                    "VALUES (:session_id, :event_type, :payload_json, :created_at)",
                    merged,
                )
                conn.commit()
                pages_before = conn.execute("PRAGMA page_count").fetchone()[0]
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                conn.close()
                if HAS_FACTS:
                    start = time.perf_counter()
                    runtime_migrations.migrate_database(db_path)
                    line["backfill_ms"] = (time.perf_counter() - start) * 1000
                    conn = sqlite3.connect(db_path)
                    pages_after = conn.execute("PRAGMA page_count").fetchone()[0]
                    conn.execute("ANALYZE")
                    conn.commit()
                    conn.close()
                    line["db_kb_before"] = pages_before * page_size / 1024
                    line["facts_kb"] = (pages_after - pages_before) * page_size / 1024
                else:
                    conn = sqlite3.connect(db_path)
                    conn.execute("ANALYZE")
                    conn.commit()
                    conn.close()
            storage = build_runtime_storage(db_path, env={}, **options)
            try:
                sessions = storage.sessions
                with_index = readers(sessions, target, runs)
                print(f"{backend} {size:>6} readers: {fmt(with_index)}", flush=True)
                # Write cost: appends through the store, after the bulk history.
                samples = []
                for turn in range(1000):
                    payload = payload_for(uuid.uuid4().hex, 1, turn)
                    start = time.perf_counter()
                    sessions.append_event(
                        session_id=writer, event_type="run.running", payload=payload
                    )
                    samples.append((time.perf_counter() - start) * 1000)
                line["append_mean_us"] = statistics.mean(samples) * 1000
                line["append_p50_us"] = statistics.median(samples) * 1000
                if HAS_FACTS:
                    sessions._backend.execute_count("DROP INDEX idx_event_run_facts_run")
                    without = readers(sessions, target, runs)
                    print(
                        f"{backend} {size:>6} readers without run-key index: {fmt(without)}",
                        flush=True,
                    )
            finally:
                storage.close()
            print(f"{backend} {size:>6} costs: {fmt(line)}", flush=True)
        finally:
            if admin is not None:
                import sqlalchemy

                with admin.begin() as conn:
                    conn.execute(sqlalchemy.text(f'DROP SCHEMA "{schema}" CASCADE'))
                admin.dispose()


if __name__ == "__main__":
    main()
