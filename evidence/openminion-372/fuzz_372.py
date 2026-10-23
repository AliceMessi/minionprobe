"""Differential probe for #372: run status readers on random payload values.

Run it once with dev's `src` and once with the PR's `src` on PYTHONPATH, with the
same seed, and compare the JSON. Sessions stay below the 2,000-event window, so
dev's window bug is not involved. Optional: OPENMINION_TEST_POSTGRES_URL runs
the PR side on Postgres too (compare it with the SQLite output of dev).

    python fuzz_372.py SEED SESSIONS [sqlite|postgres] > out.json
"""

import json
import os
import random
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path
from urllib.parse import quote_plus

from openminion.modules.storage.runtime.context import build_runtime_storage
from openminion.modules.task.run import (
    list_session_run_events,
    list_session_runs,
    resolve_thread_lifecycle,
)

PADDING = [chr(c) for c in range(0x3001) if chr(c).isspace()] + ["", "", "", "​"]
EVENT_TYPES = [
    "run.queued",
    "run.running",
    "run.waiting_tool",
    "run.failed",
    "run.completed",
    "run.cancelled",
    "run.cancel_requested",
    "run. failed",
    "RUN.failed",
    "Run.completed",
    "client.attach",
    "client.detached",
    "response.persisted",
    "response.delivered",
    "tool.call",
]
STATES = ["queued", "running", "failed", "completed", "cancelled", "bogus", "RUNNING", "Failed"]


def pad(text: str, rng: random.Random) -> str:
    return "".join(rng.choice(PADDING) for _ in range(rng.randint(0, 2))) + text + "".join(
        rng.choice(PADDING) for _ in range(rng.randint(0, 2))
    )


def value(rng: random.Random, words: list[str]) -> object:
    roll = rng.random()
    if roll < 0.55:
        return pad(rng.choice(words), rng)
    return rng.choice([None, True, False, 0, 1, 7, "7", "", {}, [], "None", "True"])


def main() -> None:
    # A deterministic clock: 1 s per append, 7 s back every 5th, with ties.
    base = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)
    ticks = iter(range(10**9))

    def tick() -> str:
        index = next(ticks)
        return (base + timedelta(seconds=index // 2 - 8 * (index // 10))).isoformat()

    with patch(
        "openminion.modules.storage.runtime.session_store.lifecycle.utc_now_iso",
        side_effect=tick,
    ):
        _main()


def _main() -> None:
    seed, count = int(sys.argv[1]), int(sys.argv[2])
    backend = sys.argv[3] if len(sys.argv) > 3 else "sqlite"
    rng = random.Random(seed)
    out = []
    with tempfile.TemporaryDirectory() as directory:
        options = {}
        if backend == "postgres":
            import sqlalchemy

            base = os.environ["OPENMINION_TEST_POSTGRES_URL"]
            schema = f"fuzz372_{uuid.uuid4().hex[:10]}"
            with sqlalchemy.create_engine(base).begin() as conn:
                conn.execute(sqlalchemy.text(f'CREATE SCHEMA "{schema}"'))
            separator = "&" if "?" in base else "?"
            url = f"{base}{separator}options={quote_plus(f'-csearch_path={schema}')}"
            options = {"record_backend": "record.postgres", "record_backend_options": {"url": url}}
        storage = build_runtime_storage(Path(directory) / "openminion.db", env={}, **options)
        try:
            sessions = storage.sessions
            for index in range(count):
                session = sessions.resolve_session(agent_id="main", channel="console", target=f"s{index}")
                for _ in range(rng.randint(5, 60)):
                    payload = {}
                    for key, words in (
                        ("run_id", ["r1", "r2", "r3"]),
                        ("state", STATES),
                        ("step", ["step-a", "step-b"]),
                        ("error", ["boom", "late"]),
                        ("conversation_id", ["conv", session.id]),
                        ("thread_id", ["t1", "t2"]),
                        ("invocation_id", ["inv-1", "inv-2"]),
                        ("attach_id", ["a1", "a2"]),
                        ("attach_role", ["writer", "observer", "WRITER"]),
                    ):
                        if rng.random() < 0.7:
                            payload[key] = value(rng, words)
                    # Values that are not JSON, or not text in Postgres.
                    odd = [float("nan"), float("inf"), float("-inf"), "bin" + chr(0), "a" + chr(92) + "u0000b"]
                    if rng.random() < 0.3:
                        payload["extra"] = rng.choice([*odd, 1.5, "loss: NaN, retry"])
                    if rng.random() < 0.1:
                        payload[rng.choice(["error", "step"])] = rng.choice([*odd, "loss: NaN, retry"])
                    sessions.append_event(
                        session_id=session.id, event_type=rng.choice(EVENT_TYPES), payload=payload
                    )
                runs = [
                    [r.run_id, r.state, r.current_step, r.error, r.event_count, bool(r.started_at), bool(r.ended_at)]
                    for r in list_session_runs(sessions, session_id=session.id, limit=50)
                ]
                events = {
                    run_id: [e.id for e in list_session_run_events(sessions, session_id=session.id, run_id=run_id)]
                    for run_id in ("r1", "r2", "r3", "7", "None", "True", "[]", "{}")
                }
                threads = {}
                for conversation_id in ("conv", session.id, ""):
                    for thread_id in ("t1", "t2"):
                        p = resolve_thread_lifecycle(
                            sessions, session_id=session.id, conversation_id=conversation_id, thread_id=thread_id
                        )
                        threads[f"{conversation_id == session.id}/{conversation_id == 'conv'}/{thread_id}"] = [
                            p.latest_run_id,
                            p.latest_run_state,
                            p.invocation_id,
                            p.invocation_source_event_id,
                            p.writer_attach_id,
                            p.latest_event_id,
                            p.delivery_state,
                            p.thread_state,
                        ]
                out.append({"runs": runs, "events": events, "threads": threads})
        finally:
            storage.close()
    print(json.dumps(out, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
