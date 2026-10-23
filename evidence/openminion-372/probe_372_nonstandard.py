"""#372: run status readers on payload values that are not standard JSON text.

`metadata_json` (json.dumps defaults) stores NaN and Infinity as bare tokens and
a NUL as \\u0000. Run with a tree's `src` on PYTHONPATH:

    python probe_372_nonstandard.py sqlite|postgres

Prints, per case, what each reader returns or the error it raises.
"""

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote_plus

from openminion.modules.storage.runtime.context import build_runtime_storage
from openminion.modules.task.run import (
    append_run_state_event,
    list_session_run_events,
    list_session_runs,
    resolve_thread_lifecycle,
)

NUL = chr(0)
CASES = {
    "error NaN": {"error": float("nan")},
    "step Infinity": {"step": float("inf")},
    "other key -Infinity": {"latency": float("-inf")},
    "error with NUL": {"error": f"bad{NUL}byte"},
    "other key with NUL": {"output": f"bin{NUL}ary"},
}


def outcome(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - the error is the observation
        return f"{type(exc).__name__}: {str(exc).splitlines()[0][:90]}"


def main() -> None:
    backend = sys.argv[1]
    options = {}
    if backend == "postgres":
        import sqlalchemy

        base = os.environ["OPENMINION_TEST_POSTGRES_URL"]
        schema = f"probe372_{uuid.uuid4().hex[:10]}"
        with sqlalchemy.create_engine(base).begin() as conn:
            conn.execute(sqlalchemy.text(f'CREATE SCHEMA "{schema}"'))
        separator = "&" if "?" in base else "?"
        url = f"{base}{separator}options={quote_plus(f'-csearch_path={schema}')}"
        options = {"record_backend": "record.postgres", "record_backend_options": {"url": url}}
    with tempfile.TemporaryDirectory() as directory:
        storage = build_runtime_storage(Path(directory) / "openminion.db", env={}, **options)
        try:
            sessions = storage.sessions
            out = {}
            for name, payload in CASES.items():
                session = sessions.resolve_session(agent_id="main", channel="console", target=name)
                common = dict(session_id=session.id, run_id="r", current_step="s",
                              conversation_id="conv", thread_id="t")
                append_run_state_event(sessions, **common, state="queued", payload={"error": "earlier"})
                append_run_state_event(sessions, **common, state="running", payload=payload)
                out[name] = {
                    "runs": outcome(lambda: [[r.run_id, r.state, r.current_step, r.error, r.event_count]
                                             for r in list_session_runs(sessions, session_id=session.id)]),
                    "run_events": outcome(lambda: len(list_session_run_events(
                        sessions, session_id=session.id, run_id="r"))),
                    "thread": outcome(lambda: (lambda p: [p.latest_run_id, p.latest_run_state])(
                        resolve_thread_lifecycle(sessions, session_id=session.id,
                                                 conversation_id="conv", thread_id="t"))),
                }
        finally:
            storage.close()
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
