"""Run readers with run events whose type differs only in case (`run.` vs `RUN.`/`Run.`).

One session with five events, written with SessionStore.append_event, the store
method behind POST /sessions/{id}/events, which keeps the event type as given:

  1 run.queued     run_id r1, state queued, invocation_id inv1
  2 run.running    run_id r1 (no state field: the state comes from the suffix)
  3 RUN.failed     run_id r1, state failed, error boom
  4 run.queued     run_id r2, state queued
  5 Run.completed  run_id r2 (no state field)

All five carry conversation_id "conv" and thread_id "t". The probe prints what
each run reader returns on SQLite and, when OPENMINION_TEST_POSTGRES_URL is set,
on PostgreSQL (one new schema, dropped at the end). It uses only APIs that exist
in both openminion 0.1.10 and the current tree, so the same script runs on both.

Usage: python probe_run_event_type_case.py <work_dir>
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import quote_plus

import openminion
from openminion.modules.storage.runtime.context import build_runtime_storage
from openminion.modules.task.run import (
    list_session_run_events,
    list_session_runs,
    resolve_invocation_terminal,
    resolve_thread_lifecycle,
)

EVENTS = (
    ("run.queued", {"run_id": "r1", "state": "queued", "invocation_id": "inv1"}),
    ("run.running", {"run_id": "r1"}),
    ("RUN.failed", {"run_id": "r1", "state": "failed", "error": "boom"}),
    ("run.queued", {"run_id": "r2", "state": "queued"}),
    ("Run.completed", {"run_id": "r2"}),
)


def readers(storage) -> dict:
    sessions = storage.sessions
    session = sessions.resolve_session(agent_id="main", channel="console", target="case")
    written = [
        sessions.append_event(
            session_id=session.id,
            event_type=event_type,
            payload={**payload, "conversation_id": "conv", "thread_id": "t"},
        )
        for event_type, payload in EVENTS
    ]
    sid = session.id
    lifecycle = resolve_thread_lifecycle(
        sessions, session_id=sid, conversation_id="conv", thread_id="t"
    )
    terminal = resolve_invocation_terminal(
        sessions,
        session_id=sid,
        trigger_event=written[2],
        conversation_id="conv",
        thread_id="t",
    )
    return {
        "stored event types": [event.event_type for event in written],
        "list_session_runs": [
            {
                "run_id": run.run_id,
                "state": run.state,
                "event_count": run.event_count,
                "ended": bool(run.ended_at),
                "error": run.error,
            }
            for run in list_session_runs(sessions, session_id=sid)
        ],
        "list_session_run_events r1": [
            f"{event.event_type} -> {event.state}"
            for event in list_session_run_events(sessions, session_id=sid, run_id="r1")
        ],
        "list_session_run_events r2": [
            f"{event.event_type} -> {event.state}"
            for event in list_session_run_events(sessions, session_id=sid, run_id="r2")
        ],
        "resolve_thread_lifecycle conv/t": {
            "latest_run_id": lifecycle.latest_run_id,
            "latest_run_state": lifecycle.latest_run_state,
            "thread_state": lifecycle.thread_state,
        },
        "resolve_invocation_terminal (trigger: event 3 RUN.failed)": (
            None
            if terminal is None
            else {"resolved_state": terminal.resolved_state, "source": terminal.source_event_type}
        ),
        "count_events(event_type_prefix='run.')": sessions.count_events(
            session_id=sid, event_type_prefix="run."
        ),
    }


def main() -> None:
    logging.disable(logging.CRITICAL)
    for key in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT", "OPENMINION_CONFIG_PATH"):
        os.environ.pop(key, None)
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    rows: dict = {}
    storage = build_runtime_storage(work / f"case-{tag}.db", env={})
    try:
        rows["SQLite"] = readers(storage)
    finally:
        storage.close()
    base = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()
    if base:
        import sqlalchemy as sa

        schema = f"probe_case_{tag}"
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
                rows["PostgreSQL"] = readers(storage)
            finally:
                storage.close()
        finally:
            with admin.begin() as conn:
                conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            admin.dispose()
    else:
        rows["PostgreSQL"] = "skipped: OPENMINION_TEST_POSTGRES_URL not set"
    for backend, result in rows.items():
        print(backend)
        if isinstance(result, str):
            print(f"  {result}")
            continue
        for reader, value in result.items():
            print(f"  {reader}: {json.dumps(value)}")
    print("openminion", openminion.__version__, "from", source(), "| python", sys.version.split()[0], sys.platform)


def source() -> str:
    """`checkout <sha>` for a source tree, else `installed package`."""
    import subprocess

    root = Path(openminion.__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return "installed package"
    sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short=8", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    return f"checkout {sha}"


if __name__ == "__main__":
    main()
