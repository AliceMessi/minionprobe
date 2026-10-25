"""PostgreSQL plans of `summarize_recent_runs` and `list_run_events` at a larger size (#396).

Writes one PostgreSQL schema with SESSIONS sessions whose turns are interleaved
in time (one turn of each session in turn), 12 events per turn, half of them
`run.*`. The probe session is one in the middle, so other sessions' events are
newer than its newest event. After ANALYZE it prints, for scan_limit 200 and
for the default 2000, `EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF,
SUMMARY OFF)` of the exact SQL `list_session_runs` sends, and the median wall
time of 15 calls of `list_session_runs` and of `list_session_run_events`.

Usage: PYTHONPATH=src:. python plans_396_pg.py <sessions> <turns_per_session>
Needs OPENMINION_TEST_POSTGRES_URL and the postgres extra.
"""

from __future__ import annotations

import inspect
import logging
import os
import statistics
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote_plus

import sqlalchemy as sa

from openminion.modules.storage.runtime.context import build_runtime_storage
from openminion.modules.storage.runtime.session_store.backend import (
    RuntimeSessionStoreBackend,
)
from openminion.modules.task.run import list_session_run_events, list_session_runs

TURN = (
    ("run.queued", "queued"),
    ("run.running", "running"),
    ("tool.call", ""),
    ("tool.result", ""),
    ("run.waiting_tool", ""),
    ("client.attach", ""),
    ("run.running", ""),
    ("run.responding", ""),
    ("run.completed", "completed"),
    ("response.persisted", ""),
    ("response.delivered", ""),
    ("session.note", ""),
)


def main() -> None:
    logging.disable(logging.CRITICAL)
    sessions_count, turns = int(sys.argv[1]), int(sys.argv[2])
    base = os.environ["OPENMINION_TEST_POSTGRES_URL"].strip()
    tag = uuid.uuid4().hex[:8]
    schema = f"plans_396_pg_{tag}"
    admin = sa.create_engine(base)
    with admin.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    work = Path(f"/tmp/plans-pg-{tag}")
    work.mkdir(parents=True, exist_ok=True)
    try:
        sep = "&" if "?" in base else "?"
        url = f"{base}{sep}options={quote_plus(f'-csearch_path={schema}')}"
        storage = build_runtime_storage(
            work / "unused.db",
            env={},
            record_backend="record.postgres",
            record_backend_options={"url": url},
        )
        try:
            run(storage, url, sessions_count, turns)
        finally:
            storage.close()
    finally:
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def run(storage, url: str, sessions_count: int, turns: int) -> None:
    sessions = storage.sessions
    ids = [
        sessions.resolve_session(agent_id="main", channel="console", target=f"s{i}").id
        for i in range(sessions_count)
    ]
    probe = ids[sessions_count // 2]
    # Every event goes through append_event, so each has its run facts row.
    started = time.perf_counter()
    for turn in range(turns):
        for session_id in ids:
            for event_type, state in TURN:
                payload = {"run_id": f"r{turn}", "conversation_id": "c", "thread_id": "t"}
                if state:
                    payload["state"] = state
                sessions.append_event(
                    session_id=session_id, event_type=event_type, payload=payload
                )
    total = sessions.event_high_water(session_id=ids[-1])
    print(
        f"sessions {sessions_count}, turns per session {turns}, events {total}, "
        f"probe session index {sessions_count // 2}; seeded in "
        f"{time.perf_counter() - started:.0f} s"
    )
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("ANALYZE"))
    captured = {}
    real = RuntimeSessionStoreBackend.query_dicts

    def recording(self, sql, params=None):
        caller = inspect.stack()[1].function
        if caller in ("summarize_recent_runs", "list_run_events"):
            captured[caller] = (sql, tuple(params or ()))
        return real(self, sql, params)

    for scan_limit in (200, 2000):
        RuntimeSessionStoreBackend.query_dicts = recording
        try:
            list_session_runs(sessions, session_id=probe, limit=20, scan_limit=scan_limit)
            list_session_run_events(sessions, session_id=probe, run_id="r5", limit=200)
        finally:
            RuntimeSessionStoreBackend.query_dicts = real
        for reader in ("summarize_recent_runs", "list_run_events"):
            if reader == "list_run_events" and scan_limit != 200:
                continue
            sql, params = captured[reader]
            print(f"\n-- {reader}" + (f", scan_limit {scan_limit}" if reader == "summarize_recent_runs" else ""))
            rows = sessions._backend.query_dicts(
                "EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql,
                params,
            )
            for row in rows:
                print("   | " + str(next(iter(row.values()))))
        timings = []
        for _ in range(15):
            t0 = time.perf_counter()
            list_session_runs(sessions, session_id=probe, limit=20, scan_limit=scan_limit)
            timings.append((time.perf_counter() - t0) * 1000)
        print(f"-- list_session_runs(scan_limit={scan_limit}) median of 15: {statistics.median(timings):.1f} ms")
    timings = []
    for _ in range(15):
        t0 = time.perf_counter()
        list_session_run_events(sessions, session_id=probe, run_id="r5", limit=200)
        timings.append((time.perf_counter() - t0) * 1000)
    print(f"-- list_session_run_events median of 15: {statistics.median(timings):.1f} ms")
    engine.dispose()


if __name__ == "__main__":
    main()
