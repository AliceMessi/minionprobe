"""Query plans of the two run readers, `list_run_events` and `summarize_recent_runs` (#396).

Seeds one runtime store with two sessions of 2,400 events each: interleaved
runs of `run.*` events (some `RUN.`/`Run.` types among them), tool events and
thread events. The probe session gets the same events in the same order.
Then it calls `list_session_run_events` and `list_session_runs` and captures
the exact SQL and parameters each reader sends to the store, and prints:
- SQLite: `EXPLAIN QUERY PLAN` of each;
- PostgreSQL (when OPENMINION_TEST_POSTGRES_URL is set; one new schema, dropped
  at the end): `ANALYZE`, then `EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF,
  SUMMARY OFF)` of each, so the plan shows the actual rows of every node;
- the rows each reader returned and the rows the readers read in total.

Run from an openminion checkout: PYTHONPATH=src:. python plans_396.py <work_dir>
The same script runs on the branch head and on `dev` sources.
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import quote_plus

from openminion.modules.storage.runtime.context import build_runtime_storage
from openminion.modules.storage.runtime.session_store.backend import (
    RuntimeSessionStoreBackend,
)
from openminion.modules.task.run import list_session_run_events, list_session_runs

READERS = ("list_run_events", "summarize_recent_runs")


def seed(sessions, session_id: str) -> None:
    for turn in range(200):
        run_id = f"run-{turn}"
        long_run = "long"
        types = [
            ("run.queued", {"run_id": run_id, "state": "queued", "invocation_id": f"i{turn}"}),
            ("run.running", {"run_id": long_run, "state": "running", "step": f"s{turn}"}),
            ("tool.call", {"run_id": run_id, "name": "lookup"}),
            ("tool.result", {"run_id": run_id, "output": "ok"}),
            ("run.waiting_tool", {"run_id": run_id}),
            ("client.attach", {"attach_id": f"a{turn}", "attach_role": "writer"}),
            ("run.running", {"run_id": run_id, "step": "answer"}),
            ("RUN.failed" if turn % 10 == 3 else "run.responding", {"run_id": run_id}),
            ("Run.completed" if turn % 10 == 7 else "run.completed", {"run_id": run_id}),
            ("response.persisted", {"run_id": run_id}),
            ("response.delivered", {"run_id": run_id}),
            ("session.note", {"note": turn}),
        ]
        for event_type, payload in types:
            sessions.append_event(
                session_id=session_id,
                event_type=event_type,
                payload={"conversation_id": "conv", "thread_id": f"t{turn % 3}", **payload},
            )


def capture(sessions, session_id: str):
    captured: dict[str, tuple[str, tuple]] = {}
    rows_read: list[int] = []
    real = RuntimeSessionStoreBackend.query_dicts

    def recording(self, sql, params=None):
        caller = inspect.stack()[1].function
        if caller in READERS:
            captured[caller] = (sql, tuple(params or ()))
        rows = real(self, sql, params)
        rows_read.append(len(rows))
        return rows

    RuntimeSessionStoreBackend.query_dicts = recording
    try:
        events = list_session_run_events(sessions, session_id=session_id, run_id="long", limit=50)
        runs = list_session_runs(sessions, session_id=session_id, limit=20, scan_limit=200)
    finally:
        RuntimeSessionStoreBackend.query_dicts = real
    return captured, {
        "list_session_run_events('long', limit=50)": len(events),
        "list_session_runs(limit=20, scan_limit=200)": len(runs),
        "rows read by both readers": sum(rows_read),
    }


def report(name: str, storage, explain) -> None:
    sessions = storage.sessions
    other = sessions.resolve_session(agent_id="main", channel="console", target="other")
    probe = sessions.resolve_session(agent_id="main", channel="console", target="plans")
    seed(sessions, other.id)
    seed(sessions, probe.id)
    captured, counts = capture(sessions, probe.id)
    print(f"== {name}")
    for reader in READERS:
        sql, params = captured[reader]
        print(f"-- {reader}")
        print("   SQL: " + " ".join(sql.split()))
        print(f"   parameters: {len(params)}")
        for line in explain(sessions._backend, sql, params):
            print(f"   | {line}")
    for key, value in counts.items():
        print(f"-- {key}: {value}")


def sqlite_explain(backend, sql, params):
    rows = backend.query_dicts("EXPLAIN QUERY PLAN " + sql, params)
    depth: dict[int, int] = {0: -1}
    lines = []
    for row in rows:
        level = depth.get(int(row["parent"]), -1) + 1
        depth[int(row["id"])] = level
        lines.append("  " * level + str(row["detail"]))
    return lines


def postgres_explain(backend, sql, params):
    backend.execute_count("ANALYZE", ())
    rows = backend.query_dicts(
        "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) " + sql, params
    )
    return [str(next(iter(row.values()))) for row in rows]


def main() -> None:
    logging.disable(logging.CRITICAL)
    for key in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT", "OPENMINION_CONFIG_PATH"):
        os.environ.pop(key, None)
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    storage = build_runtime_storage(work / f"plans-{tag}.db", env={})
    try:
        report("SQLite", storage, sqlite_explain)
    finally:
        storage.close()
    base = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()
    if not base:
        print("== PostgreSQL: skipped, OPENMINION_TEST_POSTGRES_URL not set")
        return
    import sqlalchemy as sa

    schema = f"plans_396_{tag}"
    admin = sa.create_engine(base)
    with admin.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    try:
        sep = "&" if "?" in base else "?"
        url = f"{base}{sep}options={quote_plus(f'-csearch_path={schema}')}"
        storage = build_runtime_storage(
            work / f"unused-{tag}.db",
            env={},
            record_backend="record.postgres",
            record_backend_options={"url": url},
        )
        try:
            report("PostgreSQL", storage, postgres_explain)
        finally:
            storage.close()
    finally:
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


if __name__ == "__main__":
    main()
