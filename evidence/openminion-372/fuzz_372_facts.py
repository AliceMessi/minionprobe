"""Differential probe for #372: run status readers on random payload values.

Run it once with dev's `src` and once with the PR's `src` on PYTHONPATH, with the
same seed and backend, and compare the JSON. Sessions stay below the
2,000-event window, so dev's window bug is not involved. The payloads include
the values `metadata_json` stores that are not standard JSON text: NaN and
Infinity as bare tokens, and NUL as its escape, in run events and in unrelated
tool events. OPENMINION_TEST_POSTGRES_URL is needed for `postgres`. On
Postgres the requested thread ids hold no NUL: psycopg takes no NUL in a text
parameter, so the message read of `resolve_thread_lifecycle` rejects one on
dev too; stored NUL values are written and read on both backends.

    python fuzz_372_facts.py SEED SESSIONS sqlite|postgres > out.json
"""

import json
import os
import random
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote_plus

from openminion.modules.storage.runtime.context import build_runtime_storage
from openminion.modules.task.run import (
    list_session_run_events,
    list_session_runs,
    resolve_thread_lifecycle,
)

NUL = chr(0)
NAN, INF = float("nan"), float("inf")
PADDING = [chr(c) for c in range(0x3001) if chr(c).isspace()]
PADDING += ["", "", "", chr(0x200B)]
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
    "tool.result",
]
STATES = ["queued", "running", "failed", "completed", "cancelled", "bogus", "RUNNING"]
OTHER_VALUES = [None, True, False, 0, 1, 7, "7", "", {}, [], "None", "True"]
NONSTANDARD = [NAN, INF, -INF, NUL, f"x{NUL}y", [NAN], {"v": INF}]
RUN_IDS = ["r1", "r2", "r3", "trace: NaN", "trace: 1e999", "[NaN]", f"r{NUL}1"]
THREAD_IDS = ["t1", "t2", "trace, Infinity", f"t{NUL}"]
QUERIED_RUNS = [
    "r1",
    "r2",
    "r3",
    "7",
    "None",
    "True",
    "[]",
    "{}",
    "trace: NaN",
    "trace: 1e999",
    "[NaN]",
    f"r{NUL}1",
    "nan",
    "inf",
    "-inf",
    "[nan]",
    "{'v': inf}",
    f"x{NUL}y",
]


def pad(text: str, rng: random.Random) -> str:
    left = "".join(rng.choice(PADDING) for _ in range(rng.randint(0, 2)))
    right = "".join(rng.choice(PADDING) for _ in range(rng.randint(0, 2)))
    return left + text + right


def value(rng: random.Random, words: list[str]) -> object:
    roll = rng.random()
    if roll < 0.5:
        return pad(rng.choice(words), rng)
    if roll < 0.75:
        return rng.choice(OTHER_VALUES)
    return rng.choice(NONSTANDARD)


def main() -> None:
    # A deterministic clock: 1 s per two appends, 8 s back every tenth.
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
        options: dict = {}
        if backend == "postgres":
            import sqlalchemy

            base = os.environ["OPENMINION_TEST_POSTGRES_URL"]
            schema = f"fuzz372_{uuid.uuid4().hex[:10]}"
            with sqlalchemy.create_engine(base).begin() as conn:
                conn.execute(sqlalchemy.text(f'CREATE SCHEMA "{schema}"'))
            separator = "&" if "?" in base else "?"
            search_path = quote_plus(f"-csearch_path={schema}")
            options = {
                "record_backend": "record.postgres",
                "record_backend_options": {
                    "url": f"{base}{separator}options={search_path}"
                },
            }
        storage = build_runtime_storage(
            Path(directory) / "openminion.db", env={}, **options
        )
        try:
            sessions = storage.sessions
            for index in range(count):
                session = sessions.resolve_session(
                    agent_id="main", channel="console", target=f"s{index}"
                )
                for _ in range(rng.randint(5, 60)):
                    payload: dict = {}
                    for key, words in (
                        ("run_id", RUN_IDS),
                        ("state", STATES),
                        ("step", ["step-a", "step-b"]),
                        ("error", ["boom", "late"]),
                        ("conversation_id", ["conv", session.id]),
                        ("thread_id", THREAD_IDS),
                        ("invocation_id", ["inv-1", "inv-2"]),
                        ("attach_id", ["a1", "a2"]),
                        ("attach_role", ["writer", "observer", "WRITER"]),
                    ):
                        if rng.random() < 0.7:
                            payload[key] = value(rng, words)
                    # Unrelated keys: token-looking text, escapes, NaN, NUL.
                    if rng.random() < 0.4:
                        payload["extra"] = rng.choice(
                            [
                                "loss: NaN, retry",
                                "[Infinity]",
                                "a" + chr(92) + "u0000b",
                                1.5,
                                NAN,
                                -INF,
                                f"bin{NUL}ary",
                            ]
                        )
                    sessions.append_event(
                        session_id=session.id,
                        event_type=rng.choice(EVENT_TYPES),
                        payload=payload,
                    )
                runs = [
                    [
                        r.run_id,
                        r.state,
                        r.current_step,
                        r.error,
                        r.event_count,
                        r.started_at,
                        r.ended_at,
                    ]
                    for r in list_session_runs(sessions, session_id=session.id, limit=50)
                ]
                events = {
                    run_id: [
                        e.id
                        for e in list_session_run_events(
                            sessions, session_id=session.id, run_id=run_id
                        )
                    ]
                    for run_id in QUERIED_RUNS
                }
                threads = {}
                requested = [t for t in THREAD_IDS if backend == "sqlite" or NUL not in t]
                for conversation_id in ("conv", session.id, ""):
                    for thread_id in requested:
                        p = resolve_thread_lifecycle(
                            sessions,
                            session_id=session.id,
                            conversation_id=conversation_id,
                            thread_id=thread_id,
                        )
                        label = (
                            f"{conversation_id == session.id}/"
                            f"{conversation_id == 'conv'}/{thread_id}"
                        )
                        threads[label] = [
                            p.latest_run_id,
                            p.latest_run_state,
                            p.invocation_id,
                            p.invocation_source_event_id,
                            p.writer_attach_id,
                            p.latest_event_id,
                            p.delivery_state,
                            p.thread_state,
                            p.qualifier,
                            p.updated_at,
                        ]
                out.append({"runs": runs, "events": events, "threads": threads})
        finally:
            storage.close()
    print(json.dumps(out, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
