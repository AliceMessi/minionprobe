"""Throwaway prototype of option (d) for openminion#406 (not part of the PR).

A storage-owned derived table holding, per session, every top-level member of
the stored metadata whose value is a JSON string:

    session_metadata_strings(session_id, key, value, entry_digest)

- `key` and `value` are the member name and the string value as their JSON
  string text (`json.dumps`, ASCII, no NUL byte), so equality is exact on
  both backends, including NUL, lone surrogates and non-ASCII text.
- `entry_digest` (16 hex characters of SHA-256 over key + value) is only the
  index entry; the query still compares `key` and `value` in full.
- The rows come from the stored `metadata_json` read back with
  `parse_json_object` (`json.loads`), as `SessionRecord.metadata` is.

The script simulates runtime migration 15 (DDL plus a Python backfill in
batches of 1,000 inside one transaction) on stores built by dev, then checks:

    PYTHONPATH=src python proto_d_406.py differential [rows] [filters] [seed]
    PYTHONPATH=src python proto_d_406.py edge
    PYTHONPATH=src python proto_d_406.py timing [rows]

Needs OPENMINION_TEST_POSTGRES_URL; each run uses its own schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import statistics
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote_plus

from openminion.modules.storage.runtime.context import build_runtime_storage
from openminion.modules.storage.runtime.session_store.rows import (
    SESSION_COLUMNS,
    metadata_json,
    parse_json_object,
    row_to_session,
)

NUL = chr(0)
BS = chr(92)
NAN = float("nan")
INF = float("inf")
BATCH = 1000

DDL = (
    """
    CREATE TABLE session_metadata_strings (
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        entry_digest TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_session_metadata_strings_entry ON session_metadata_strings(entry_digest, session_id)",
    "CREATE INDEX idx_session_metadata_strings_session ON session_metadata_strings(session_id)",
)


def encode(text: str) -> str:
    return json.dumps(text)


def entry_digest(key: str, value: str) -> str:
    return hashlib.sha256((key + value).encode("ascii")).hexdigest()[:16]


def metadata_string_rows(session_id: str, stored: str) -> list[dict]:
    rows = []
    for name, value in parse_json_object(stored).items():
        if isinstance(value, str):
            key, encoded = encode(name), encode(value)
            rows.append(
                {"session_id": session_id, "key": key, "value": encoded,
                 "entry_digest": entry_digest(key, encoded)}
            )
    return rows


def backfill(record_store) -> int:
    """Migration 15 backfill: sessions in id order, BATCH at a time."""
    after, written = "", 0
    while True:
        batch = record_store.query_dicts(
            "SELECT id, metadata_json FROM sessions WHERE id > ? ORDER BY id ASC LIMIT ?",
            (after, BATCH),
        )
        if not batch:
            return written
        facts = []
        for row in batch:
            facts.extend(metadata_string_rows(str(row["id"]), str(row["metadata_json"])))
        if facts:
            record_store.insert_many("session_metadata_strings", facts)
        written += len(facts)
        after = str(batch[-1]["id"])


def migrate_15(record_store) -> int:
    with record_store.transaction():
        for statement in DDL:
            record_store.execute_count(statement)
        return backfill(record_store)


def replace_metadata(record_store, session_id: str, metadata: dict, now: str) -> None:
    """A metadata write: the session row and its derived rows in one transaction."""
    stored = metadata_json(metadata)
    record_store.execute_count(
        "UPDATE sessions SET metadata_json = ?, updated_at = ? WHERE id = ?", (stored, now, session_id)
    )
    record_store.execute_count("DELETE FROM session_metadata_strings WHERE session_id = ?", (session_id,))
    rows = metadata_string_rows(session_id, stored)
    if rows:
        record_store.insert_many("session_metadata_strings", rows)


def filter_clauses(metadata_filter: dict) -> tuple[list[str], list[object], list[tuple[str, str]]]:
    clauses, params, filters = [], [], []
    for raw_key, raw_value in metadata_filter.items():
        name = str(raw_key or "").strip()
        if not name:
            continue
        value = raw_value or ""
        if not isinstance(value, str):
            raise TypeError("metadata filter values must be strings")
        key, encoded = encode(name), encode(value)
        clauses.append(
            "EXISTS (SELECT 1 FROM session_metadata_strings AS m WHERE m.session_id = sessions.id "
            "AND m.entry_digest = ? AND m.key = ? AND m.value = ?)"
        )
        params.extend([entry_digest(key, encoded), key, encoded])
        filters.append((name, value))
    return clauses, params, filters


def table_list(store, *, limit: int, newest_first: bool, target, metadata_filter: dict):
    direction = "DESC" if newest_first else "ASC"
    clauses, params, _ = filter_clauses(metadata_filter)
    if target:
        clauses.insert(0, "target = ?")
        params.insert(0, target)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = store._backend.query_dicts(
        f"SELECT {SESSION_COLUMNS} FROM sessions {where} "
        f"ORDER BY updated_at {direction}, id {direction} LIMIT ?",
        [*params, limit],
    )
    return [row_to_session(row) for row in rows]


def head_list(store, *, limit: int, metadata_filter: dict):
    """This head's PostgreSQL predicate (whole-document jsonb cast), for timing."""
    clauses, params = [], []
    for key, value in metadata_filter.items():
        clauses.append("(CAST(metadata_json AS jsonb) -> CAST(? AS text)) = to_jsonb(CAST(? AS text))")
        params.extend([key, value])
    rows = store._backend.query_dicts(
        f"SELECT {SESSION_COLUMNS} FROM sessions WHERE {' AND '.join(clauses)} "
        "ORDER BY updated_at DESC, id DESC LIMIT ?",
        [*params, limit],
    )
    return [row_to_session(row) for row in rows]


def reference(all_rows: list[dict], *, limit: int, newest_first: bool, target, metadata_filter: dict):
    _, _, filters = filter_clauses(metadata_filter)
    selected = []
    for row in all_rows:
        if target and row["target"] != target:
            continue
        metadata = parse_json_object(str(row["metadata_json"]))
        if all(isinstance(metadata.get(k), str) and metadata[k] == v for k, v in filters):
            selected.append(row)
    selected.sort(key=lambda r: (r["updated_at"], r["id"]), reverse=newest_first)
    return [str(r["id"]) for r in selected[:limit]]


def schema_url(base: str, schema: str) -> str:
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}options={quote_plus(f'-csearch_path={schema}')}"


class Stores:
    def __init__(self) -> None:
        import sqlalchemy as sa

        self._sa = sa
        self._tmp = tempfile.TemporaryDirectory()
        base = os.environ["OPENMINION_TEST_POSTGRES_URL"]
        self._admin = sa.create_engine(base, future=True)
        self._schema = f"om406d_{uuid.uuid4().hex[:12]}"
        with self._admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{self._schema}"'))
        self.sqlite = build_runtime_storage(Path(self._tmp.name) / "s" / "openminion.db", env={})
        self.postgres = build_runtime_storage(
            Path(self._tmp.name) / "p" / "openminion.db", env={},
            record_backend="record.postgres",
            record_backend_options={"url": schema_url(base, self._schema)},
        )
        self.both = (("sqlite", self.sqlite), ("postgres", self.postgres))

    def close(self) -> None:
        self.sqlite.close()
        self.postgres.close()
        with self._admin.begin() as conn:
            conn.execute(self._sa.text(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE'))
        self._admin.dispose()
        self._tmp.cleanup()


ODD_KEYS = ["caf" + chr(0xE9), "a.b", "a[0]", '"a"', "a" + BS + "b", "k" + NUL + "z", chr(0xD800), "NaN", " padded "]
KEYS = ["surface", "working_dir", "k"] + ODD_KEYS
STRINGS = [
    "acp", "cli", "ACP", "acp ", "", "/home/a/p", '{"a":1}', "[draft]", "{}", "a" + NUL + "b",
    "x" + chr(0xD800), "caf" + chr(0xE9), chr(0x1F600), "trace: NaN", "1", "true", "null",
]
NON_STRINGS = [1, 1.5, True, False, None, {"a": 1}, ["acp"], [], NAN, INF, -INF]


def random_metadata(rng: random.Random) -> dict:
    metadata: dict = {}
    for key in rng.sample(KEYS + ["n", "other"], rng.randint(0, 5)):
        metadata[key] = rng.choice(STRINGS) if rng.random() < 0.65 else rng.choice(NON_STRINGS)
    return metadata


def stamps(count: int) -> list[str]:
    return [f"2026-10-25T{10 + m // 60:02d}:{m % 60:02d}:00+00:00" for m in range(max(5, count // 20))]


def insert_sessions(stores: Stores, rows: list[tuple]) -> None:
    sql = (
        "INSERT INTO sessions(id, session_key, channel, target, metadata_json, created_at, "
        "updated_at, status, last_activity_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    for _, storage in stores.both:
        with storage.record_store.transaction():
            for row in rows:
                storage.record_store.execute_count(sql, row)


def corpus(rng: random.Random, count: int) -> list[tuple]:
    times = stamps(count)
    rows = []
    for index in range(count):
        session_id = f"s-{rng.getrandbits(48):012x}-{index:06d}"
        stamp = rng.choice(times)
        if index % 997 == 0:
            stored = metadata_json({NAN: "acp", float("nan"): "cli"})
        elif index % 1499 == 0:
            stored = "[]"
        else:
            stored = metadata_json(random_metadata(rng))
        rows.append((session_id, f"key-{session_id}", "console", rng.choice(["t1", "t2"]),
                     stored, stamp, stamp, "active", stamp))
    return rows


def all_sessions(storage) -> list[dict]:
    return storage.record_store.query_dicts(f"SELECT {SESSION_COLUMNS} FROM sessions")


def metadata_hash(storage) -> str:
    rows = storage.record_store.query_dicts("SELECT id, metadata_json FROM sessions ORDER BY id")
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda r: r["id"]):
        digest.update(f"{row['id']}={row['metadata_json']}".encode("utf-8", "surrogatepass"))
    return digest.hexdigest()[:16]


def check_backfill(storage) -> int:
    stored = storage.record_store.query_dicts("SELECT session_id, key, value, entry_digest FROM session_metadata_strings")
    got = {(r["session_id"], r["key"], r["value"], r["entry_digest"]) for r in stored}
    expected = set()
    for row in all_sessions(storage):
        for fact in metadata_string_rows(str(row["id"]), str(row["metadata_json"])):
            expected.add((fact["session_id"], fact["key"], fact["value"], fact["entry_digest"]))
    return len(got ^ expected) + (len(stored) - len(got))


def differential(count: int, filters: int, seed: int) -> None:
    stores = Stores()
    try:
        rng = random.Random(seed)
        insert_sessions(stores, corpus(rng, count))
        version = stores.postgres.record_store.query_dicts("SHOW server_version")[0]["server_version"]
        out = {"postgres": version, "rows": count, "seed": seed}
        for name, storage in stores.both:
            before = metadata_hash(storage)
            start = time.perf_counter()
            written = migrate_15(storage.record_store)
            out[f"{name}_backfill_rows"] = written
            out[f"{name}_backfill_ms"] = round((time.perf_counter() - start) * 1000, 1)
            out[f"{name}_backfill_mismatches"] = check_backfill(storage)
            out[f"{name}_metadata_unchanged"] = metadata_hash(storage) == before
        try:
            out["documents_jsonb_rejects"] = stores.postgres.record_store.query_dicts(
                "SELECT count(*) AS n FROM sessions WHERE NOT pg_input_is_valid(metadata_json, 'jsonb')"
            )[0]["n"]
        except Exception as exc:  # noqa: BLE001 - PostgreSQL before 16
            out["documents_jsonb_rejects"] = type(exc).__name__
        print(json.dumps(out))
        reference_rows = all_sessions(stores.postgres)
        frng = random.Random(seed + 1)
        mismatches = {"postgres": 0, "sqlite": 0}
        over_limit = 0
        nonempty = 0
        for _ in range(filters):
            keys = frng.sample(KEYS, frng.choice([1, 1, 1, 2]))
            metadata_filter = {key: frng.choice(STRINGS) for key in keys}
            limit = frng.choice([1, 2, 3, 5, 10, 50, 100000])
            newest_first = frng.random() < 0.7
            target = frng.choice([None, None, "t1"])
            expected = reference(reference_rows, limit=limit, newest_first=newest_first,
                                 target=target, metadata_filter=metadata_filter)
            nonempty += bool(expected)
            for name, storage in stores.both:
                got = [s.id for s in table_list(storage.sessions, limit=limit, newest_first=newest_first,
                                                target=target, metadata_filter=metadata_filter)]
                over_limit += len(got) > limit
                if got != expected:
                    mismatches[name] += 1
                    if mismatches[name] <= 3:
                        print("MISMATCH", name, ascii(metadata_filter), limit, newest_first, target,
                              expected[:4], got[:4])
        print(json.dumps({"filters": filters, "nonempty_results": nonempty,
                          "mismatches_vs_json_loads_reference": mismatches,
                          "results_over_limit": over_limit}))
    finally:
        stores.close()


def edge() -> None:
    """Long members, NUL/surrogate keys and values, atomic replacement, delete."""
    stores = Stores()
    try:
        long_value = "v" * 100_000
        long_key = "k" * 5_000
        cases = {
            "s-long": {long_key: long_value, "surface": "acp"},
            "s-nul": {"k" + NUL + "z": "a" + NUL + "b", "surface": "x" + chr(0xD800)},
            "s-brace": {"surface": '{"a":1}', "working_dir": "[draft]"},
            "s-object": {"surface": {"a": 1}, "working_dir": ["x"]},
            "s-path": {"a.b": "lit", "a": {"b": "nested"}},
            "s-nan": {"surface": "acp", "n": NAN, "m": -INF},
        }
        rows = [
            (sid, f"key-{sid}", "console", "t1", metadata_json(md),
             "2026-10-25T10:00:00+00:00", "2026-10-25T10:00:00+00:00", "active",
             "2026-10-25T10:00:00+00:00")
            for sid, md in cases.items()
        ]
        insert_sessions(stores, rows)
        probes = [
            {long_key: long_value},
            {"surface": "acp"},
            {"k" + NUL + "z": "a" + NUL + "b"},
            {"surface": "x" + chr(0xD800)},
            {"surface": '{"a":1}'},
            {"working_dir": "[draft]"},
            {"a.b": "lit"},
            {"a.b": "nested"},
            {"working_dir": '["x"]'},
        ]
        for name, storage in stores.both:
            migrate_15(storage.record_store)
            print(f"## {name}: backfill mismatches {check_backfill(storage)}")
            for probe in probes:
                ids = [s.id for s in table_list(storage.sessions, limit=10, newest_first=True,
                                                 target=None, metadata_filter=probe)]
                label = {ascii(k)[:24]: ascii(v)[:24] for k, v in probe.items()}
                print(f"  filter {label}: {ids}")
            record_store = storage.record_store
            try:
                with record_store.transaction():
                    replace_metadata(record_store, "s-brace", {"surface": "new"}, "2026-10-25T11:00:00+00:00")
                    raise RuntimeError("injected failure after the replacement")
            except RuntimeError:
                pass
            after_rollback = [s.id for s in table_list(storage.sessions, limit=10, newest_first=True,
                                                        target=None, metadata_filter={"surface": '{"a":1}'})]
            print(f"  after a rolled-back replacement, old value still matches: {after_rollback}")
            with record_store.transaction():
                replace_metadata(record_store, "s-brace", {"surface": "new"}, "2026-10-25T11:00:00+00:00")
            old = [s.id for s in table_list(storage.sessions, limit=10, newest_first=True,
                                             target=None, metadata_filter={"surface": '{"a":1}'})]
            new = [s.id for s in table_list(storage.sessions, limit=10, newest_first=True,
                                             target=None, metadata_filter={"surface": "new"})]
            print(f"  after a committed replacement: old value {old}, new value {new}")
            record_store.execute_count("DELETE FROM sessions WHERE id = ?", ("s-nul",))
            left = record_store.query_dicts(
                "SELECT count(*) AS n FROM session_metadata_strings WHERE session_id = ?", ("s-nul",)
            )[0]["n"]
            print(f"  derived rows left after deleting the session row (FK cascade): {left}")
    finally:
        stores.close()


def realistic_metadata(rng: random.Random, index: int) -> dict:
    surface = rng.choices(["acp", "cli", "gateway", "focus"], weights=[5, 60, 25, 10])[0]
    project = f"/home/user/projects/p{rng.randrange(200):03d}"
    metadata = {"surface": surface, "workspace_root": project,
                "title": f"session {index} about topic {rng.randrange(1000)}", "turns": rng.randrange(50)}
    if surface == "focus":
        metadata.update({"working_dir": project, "focus_mode": True, "cwd": project})
    return metadata


def timing(count: int) -> None:
    stores = Stores()
    try:
        rng = random.Random(11)
        times = [f"2026-10-{1 + d:02d}T{h:02d}:{m:02d}:00+00:00"
                 for d in range(25) for h in range(24) for m in range(0, 60, 7)]
        rows = []
        for index in range(count):
            session_id = f"s-{rng.getrandbits(48):012x}-{index:06d}"
            stamp = rng.choice(times)
            rows.append((session_id, f"key-{session_id}", "console", "t1",
                         metadata_json(realistic_metadata(rng, index)), stamp, stamp, "active", stamp))
        insert_sessions(stores, rows)
        storage = stores.postgres
        record_store = storage.record_store
        start = time.perf_counter()
        written = migrate_15(record_store)
        print(f"backfill (migration 15 simulation): {count} sessions, {written} rows,"
              f" {(time.perf_counter() - start) * 1000:.0f} ms")
        record_store.execute_count("ANALYZE sessions")
        record_store.execute_count("ANALYZE session_metadata_strings")
        size = record_store.query_dicts(
            "SELECT pg_total_relation_size('session_metadata_strings') AS facts, "
            "pg_total_relation_size('sessions') AS sessions"
        )[0]
        print(f"size: session_metadata_strings {size['facts'] // 1024} KiB (with indexes),"
              f" sessions {size['sessions'] // 1024} KiB")

        def median_ms(fn, repeat: int = 9) -> float:
            samples = []
            for _ in range(repeat):
                begin = time.perf_counter()
                fn()
                samples.append((time.perf_counter() - begin) * 1000)
            return statistics.median(samples)

        cases = {
            "surface=acp": {"surface": "acp"},
            "surface=cli (60%)": {"surface": "cli"},
            "working_dir (rare)": {"working_dir": "/home/user/projects/p007"},
        }
        for label, metadata_filter in cases.items():
            got = table_list(storage.sessions, limit=100, newest_first=True, target=None,
                             metadata_filter=metadata_filter)
            head = head_list(storage.sessions, limit=100, metadata_filter=metadata_filter)
            same = [s.id for s in got] == [s.id for s in head]
            table_ms = median_ms(lambda: table_list(storage.sessions, limit=100, newest_first=True,
                                                    target=None, metadata_filter=metadata_filter))
            head_ms = median_ms(lambda: head_list(storage.sessions, limit=100, metadata_filter=metadata_filter))
            print(f"{label}, limit 100 | derived table {table_ms:.1f} ms | this head's cast {head_ms:.1f} ms"
                  f" | returned {len(got)} | same ids: {same}")
        clauses, params, _ = filter_clauses({"working_dir": "/home/user/projects/p007"})
        plan = record_store.query_dicts(
            f"EXPLAIN (COSTS OFF) SELECT id FROM sessions WHERE {clauses[0]} "
            "ORDER BY updated_at DESC, id DESC LIMIT 100",
            params,
        )
        print("plan (rare working_dir):")
        for line in plan:
            print("   ", list(line.values())[0])
        sample = random.Random(3).sample(rows, 300)
        samples = []
        for row in sample:
            metadata = parse_json_object(row[4])
            metadata["title"] = metadata["title"] + " (renamed)"
            begin = time.perf_counter()
            with record_store.transaction():
                replace_metadata(record_store, row[0], metadata, "2026-10-26T00:00:00+00:00")
            samples.append((time.perf_counter() - begin) * 1000)
        baseline = []
        for row in sample:
            begin = time.perf_counter()
            with record_store.transaction():
                record_store.execute_count(
                    "UPDATE sessions SET metadata_json = ?, updated_at = ? WHERE id = ?",
                    (row[4], "2026-10-26T00:00:01+00:00", row[0]),
                )
            baseline.append((time.perf_counter() - begin) * 1000)
        print(f"metadata write, median of 300: with derived rows {statistics.median(samples):.2f} ms,"
              f" row update only {statistics.median(baseline):.2f} ms")
    finally:
        stores.close()


if __name__ == "__main__":
    import sys

    mode = sys.argv[1]
    if mode == "differential":
        differential(int(sys.argv[2]) if len(sys.argv) > 2 else 3000,
                     int(sys.argv[3]) if len(sys.argv) > 3 else 400,
                     int(sys.argv[4]) if len(sys.argv) > 4 else 1)
    elif mode == "edge":
        edge()
    else:
        timing(int(sys.argv[2]) if len(sys.argv) > 2 else 50000)
