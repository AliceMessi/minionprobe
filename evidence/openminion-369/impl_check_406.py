"""Checks of the implemented #406 head (option (d)), outside the test suite.

    PYTHONPATH=src python impl_check_406.py differential [sessions] [filters] [seed]
    PYTHONPATH=src python impl_check_406.py costs [sessions]

`differential` writes sessions through the real store API (`resolve_session`
with explicit ids, `update_session_metadata`, `delete_session`) on SQLite and
PostgreSQL, with metadata PostgreSQL cannot parse, then checks that the
`session_metadata_strings` rows equal the rows derived from every stored
`metadata_json`, and that PostgreSQL `list_sessions(metadata_filter=...)`
returns the ids and order of a `json.loads` reference, never more than `limit`.

`costs` measures migration 15 on PostgreSQL: a runtime database at migration 14
with N realistic sessions, the backfill time, table sizes, filter timings and
the cost of a metadata write.

Needs OPENMINION_TEST_POSTGRES_URL; each run uses its own schema.
"""

from __future__ import annotations

import json
import os
import random
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote_plus

from openminion.modules.storage.runtime.context import build_runtime_storage
from openminion.modules.storage.runtime.migrations import (
    DEFAULT_MIGRATIONS,
    migrate_record_store,
)
from openminion.modules.storage.runtime.session_store import SessionStore
from openminion.modules.storage.runtime.session_store.metadata_strings import (
    metadata_string_rows,
)
from openminion.modules.storage.runtime.session_store.rows import (
    metadata_json,
    parse_json_object,
)

NUL = chr(0)
BS = chr(92)
NAN = float("nan")
INF = float("inf")
KEYS = ["surface", "working_dir", "k", "caf" + chr(0xE9), "a.b", "a[0]", '"q"', "a" + BS + "b",
        "k" + NUL + "z", chr(0xD800), "NaN", " padded "]
STRINGS = ["acp", "cli", "ACP", "acp ", "", "/home/a/p", '{"a":1}', "[draft]", "{}", "a" + NUL + "b",
           "x" + chr(0xD800), "caf" + chr(0xE9), chr(0x1F600), "trace: NaN", "1", "true", "null"]
NON_STRINGS = [1, 1.5, True, False, None, {"a": 1}, ["acp"], [], NAN, INF, -INF]
TARGET = "impl-check"


def schema_url(base: str, schema: str) -> str:
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}options={quote_plus(f'-csearch_path={schema}')}"


class Schema:
    def __init__(self) -> None:
        import sqlalchemy as sa

        self._sa = sa
        self.base = os.environ["OPENMINION_TEST_POSTGRES_URL"]
        self._admin = sa.create_engine(self.base, future=True)
        self.name = f"om406i_{uuid.uuid4().hex[:12]}"
        with self._admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{self.name}"'))
        self.url = schema_url(self.base, self.name)

    def drop(self) -> None:
        with self._admin.begin() as conn:
            conn.execute(self._sa.text(f'DROP SCHEMA IF EXISTS "{self.name}" CASCADE'))
        self._admin.dispose()


def random_metadata(rng: random.Random) -> dict:
    metadata: dict = {}
    for key in rng.sample(KEYS + ["n", "other"], rng.randint(0, 5)):
        metadata[key] = rng.choice(STRINGS) if rng.random() < 0.65 else rng.choice(NON_STRINGS)
    if rng.random() < 0.01:
        metadata = {NAN: rng.choice(STRINGS), float("nan"): rng.choice(STRINGS)}
    return metadata


def rows_check(sessions: SessionStore) -> int:
    stored = sessions._backend.query_dicts(
        "SELECT session_id, key, value, entry_digest FROM session_metadata_strings"
    )
    got = [(r["session_id"], r["key"], r["value"], r["entry_digest"]) for r in stored]
    expected = set()
    for row in sessions._backend.query_dicts("SELECT id, metadata_json FROM sessions"):
        for fact in metadata_string_rows(str(row["id"]), str(row["metadata_json"])):
            expected.add((fact["session_id"], fact["key"], fact["value"], fact["entry_digest"]))
    return len(set(got) ^ expected) + (len(got) - len(set(got)))


def reference(sessions: SessionStore, metadata_filter: dict, limit: int, newest_first: bool) -> list[str]:
    filters = [(str(k).strip(), v or "") for k, v in metadata_filter.items() if str(k or "").strip()]
    selected = []
    for row in sessions._backend.query_dicts(
        "SELECT id, updated_at, metadata_json FROM sessions WHERE target = ?", (TARGET,)
    ):
        metadata = parse_json_object(str(row["metadata_json"]))
        if all(isinstance(metadata.get(k), str) and metadata[k] == v for k, v in filters):
            selected.append((str(row["updated_at"]), str(row["id"])))
    selected.sort(reverse=newest_first)
    return [session_id for _, session_id in selected[:limit]]


def differential(count: int, filters: int, seed: int) -> None:
    schema = Schema()
    tmp = tempfile.TemporaryDirectory()
    stores = {
        "sqlite": build_runtime_storage(Path(tmp.name) / "s" / "openminion.db", env={}),
        "postgres": build_runtime_storage(
            Path(tmp.name) / "p" / "openminion.db", env={},
            record_backend="record.postgres", record_backend_options={"url": schema.url},
        ),
    }
    try:
        version = stores["postgres"].record_store.query_dicts("SHOW server_version")[0]["server_version"]
        out: dict = {"postgres": version, "sessions": count, "seed": seed}
        for name, storage in stores.items():
            rng = random.Random(seed)
            sessions = storage.sessions
            ids = []
            for index in range(count):
                session_id = f"s-{rng.getrandbits(32):08x}-{index:05d}"
                ids.append(session_id)
                sessions.resolve_session(agent_id="main", channel="console", target=TARGET,
                                         session_id=session_id, metadata=random_metadata(rng))
            for session_id in rng.sample(ids, count // 3):
                patch = {k: v for k, v in random_metadata(rng).items() if isinstance(k, str)}
                sessions.update_session_metadata(session_id=session_id, patch=patch)
            for session_id in rng.sample(ids, count // 20):
                sessions.delete_session(session_id)
            out[f"{name}_row_mismatches"] = rows_check(sessions)
            out[f"{name}_rows"] = sessions._backend.query_dicts(
                "SELECT count(*) AS n FROM session_metadata_strings")[0]["n"]
        try:
            out["postgres_documents_jsonb_rejects"] = stores["postgres"].record_store.query_dicts(
                "SELECT count(*) AS n FROM sessions WHERE NOT pg_input_is_valid(metadata_json, 'jsonb')"
            )[0]["n"]
        except Exception as exc:  # noqa: BLE001 - PostgreSQL before 16
            out["postgres_documents_jsonb_rejects"] = type(exc).__name__
        print(json.dumps(out))
        sessions = stores["postgres"].sessions
        frng = random.Random(seed + 1)
        mismatches = over_limit = nonempty = 0
        for _ in range(filters):
            keys = frng.sample(KEYS, frng.choice([1, 1, 1, 2]))
            metadata_filter = {key: frng.choice(STRINGS) for key in keys}
            limit = frng.choice([1, 2, 3, 5, 10, 50, 100000])
            newest_first = frng.random() < 0.7
            expected = reference(sessions, metadata_filter, limit, newest_first)
            got = [s.id for s in sessions.list_sessions(limit=limit, newest_first=newest_first,
                                                      target=TARGET, metadata_filter=metadata_filter)]
            nonempty += bool(expected)
            over_limit += len(got) > limit
            if got != expected:
                mismatches += 1
                if mismatches <= 3:
                    print("MISMATCH", ascii(metadata_filter), limit, newest_first, expected[:4], got[:4])
        print(json.dumps({"filters": filters, "nonempty_results": nonempty,
                          "postgres_mismatches_vs_json_loads_reference": mismatches,
                          "results_over_limit": over_limit}))
    finally:
        for storage in stores.values():
            storage.close()
        schema.drop()
        tmp.cleanup()


def realistic_metadata(rng: random.Random, index: int) -> dict:
    surface = rng.choices(["acp", "cli", "gateway", "focus"], weights=[5, 60, 25, 10])[0]
    project = f"/home/user/projects/p{rng.randrange(200):03d}"
    metadata = {"surface": surface, "workspace_root": project,
                "title": f"session {index} about topic {rng.randrange(1000)}", "turns": rng.randrange(50)}
    if surface == "focus":
        metadata.update({"working_dir": project, "focus_mode": True, "cwd": project})
    return metadata


def costs(count: int) -> None:
    from openminion.modules.storage.backends.postgres import RecordStorePostgres

    schema = Schema()
    record_store = RecordStorePostgres(schema.url)
    try:
        migrate_record_store(record_store, backend_type="postgres",
                             migrations=tuple(m for m in DEFAULT_MIGRATIONS if m.version < 15))
        rng = random.Random(11)
        times = [f"2026-10-{1 + d:02d}T{h:02d}:{m:02d}:00+00:00"
                 for d in range(25) for h in range(24) for m in range(0, 60, 7)]
        rows = []
        for index in range(count):
            session_id = f"s-{rng.getrandbits(48):012x}-{index:06d}"
            stamp = rng.choice(times)
            rows.append({"id": session_id, "session_key": f"key-{session_id}", "channel": "console",
                         "target": "t1", "metadata_json": metadata_json(realistic_metadata(rng, index)),
                         "created_at": stamp, "updated_at": stamp})
        with record_store.transaction():
            record_store.insert_many("sessions", rows)
        start = time.perf_counter()
        result = migrate_record_store(record_store, backend_type="postgres")
        backfill_ms = (time.perf_counter() - start) * 1000
        facts = record_store.query_dicts("SELECT count(*) AS n FROM session_metadata_strings")[0]["n"]
        print(f"migration {result.applied_versions}: {count} sessions, {facts} rows, {backfill_ms:.0f} ms")
        record_store.execute_count("ANALYZE sessions")
        record_store.execute_count("ANALYZE session_metadata_strings")
        size = record_store.query_dicts(
            "SELECT pg_total_relation_size('session_metadata_strings') AS facts, "
            "pg_total_relation_size('sessions') AS sessions")[0]
        print(f"size: session_metadata_strings {size['facts'] / 1048576:.1f} MiB with indexes,"
              f" sessions {size['sessions'] / 1048576:.1f} MiB")
        store = SessionStore(record_store)

        def median_ms(fn, repeat: int = 9) -> float:
            samples = []
            for _ in range(repeat):
                begin = time.perf_counter()
                fn()
                samples.append((time.perf_counter() - begin) * 1000)
            return statistics.median(samples)

        for label, metadata_filter in (
            ("surface=acp", {"surface": "acp"}),
            ("surface=cli (60%)", {"surface": "cli"}),
            ("rare working_dir", {"working_dir": "/home/user/projects/p007"}),
        ):
            got = store.list_sessions(limit=100, metadata_filter=metadata_filter)
            ms = median_ms(lambda: store.list_sessions(limit=100, metadata_filter=metadata_filter))
            print(f"list_sessions {label}, limit 100: {ms:.1f} ms, {len(got)} returned")
        sample = random.Random(3).sample(rows, 300)
        with_rows, update_only = [], []
        for row in sample:
            begin = time.perf_counter()
            store.update_session_metadata(session_id=row["id"], patch={"title": "renamed"})
            with_rows.append((time.perf_counter() - begin) * 1000)
        for row in sample:
            begin = time.perf_counter()
            with record_store.transaction():
                current = record_store.query_dicts(
                    "SELECT metadata_json FROM sessions WHERE id = ?", (row["id"],))[0]["metadata_json"]
                record_store.execute_count(
                    "UPDATE sessions SET metadata_json = ?, updated_at = ? WHERE id = ?",
                    (current, "2026-10-27T00:00:00+00:00", row["id"]))
            update_only.append((time.perf_counter() - begin) * 1000)
        print(f"metadata write, median of 300: update_session_metadata {statistics.median(with_rows):.2f} ms,"
              f" read and update of the row alone {statistics.median(update_only):.2f} ms")
    finally:
        record_store.close()
        schema.drop()


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "differential":
        differential(int(sys.argv[2]) if len(sys.argv) > 2 else 1500,
                     int(sys.argv[3]) if len(sys.argv) > 3 else 400,
                     int(sys.argv[4]) if len(sys.argv) > 4 else 1)
    else:
        costs(int(sys.argv[2]) if len(sys.argv) > 2 else 50000)
