"""Throwaway prototype for the openminion#406 proposal (not part of the PR).

It stores the same sessions in a SQLite and a PostgreSQL runtime store, with
`metadata_json` written by the store's own writer (`rows.metadata_json`,
`json.dumps(..., sort_keys=True)`), including documents `jsonb` cannot hold
(NaN, Infinity, an escaped NUL, a lone surrogate). Then it compares, filter by
filter, the ids and order `list_sessions` returns on SQLite (the existing
reader, unchanged) with three PostgreSQL candidates:

- `valid`: two-tier, rows routed by `pg_input_is_valid(metadata_json, 'jsonb')`
  (PostgreSQL 16 and later)
- `pattern`: two-tier, rows routed by a regular expression over the stored text
- `python`: every candidate row read in Python, in keyset pages

Two-tier: SQL compares JSON strings for the rows PostgreSQL can parse; the
other rows come back unfiltered with a flag, and the store decides them in
Python with the same `parse_json_object` every listed row already goes
through. One statement, with `LIMIT n + (rows PostgreSQL cannot parse)`, so
ordering and the limit stay exact. `shape` picks how that statement is
written: `subquery` (the default, a count subquery), `cte` or `window`.

    PYTHONPATH=src python proto_406.py differential [rows] [filters] [seed]
    PYTHONPATH=src python proto_406.py timing [rows]
    PYTHONPATH=src python proto_406.py shapes [rows]
    PYTHONPATH=src python proto_406.py construction

Filters stay inside the proposed contract (`check_contract`): plain
printable-ASCII keys without `.`, `[`, `"` or a backslash, and string values
without NUL that do not start with `{` or `[`.

Needs OPENMINION_TEST_POSTGRES_URL; each run uses its own schema.
PROTO_ROUTERS (default `valid,pattern,python`) selects the candidates.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import statistics
import sys
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
# Any document `jsonb` rejects contains one of these in its stored text
# (json.dumps writes NaN/Infinity bare and escapes NUL and surrogates).
ROUTE_PATTERN = BS + BS + "u0000|" + BS + BS + "u[dD][89a-fA-F]|NaN|Infinity"
UNSUPPORTED_KEY_CHARS = set(".[" + '"' + BS)

STRINGS = [
    "acp", "cli", "ACP", "acp ", "", "/home/a/proj", "C:" + BS + "w" + BS + "p",
    "caf" + chr(0xE9), chr(0x1F600), "a" + NUL + "b", "trace: NaN", "Infinity",
    "1", "true", "null", "a" + BS + "u0000b",
]
ODD = [NAN, INF, -INF, "x" + chr(0xD800), "y" + chr(0xDC00), "a" + NUL + "b"]
OTHER_VALUES = [1, 1.5, 0, True, False, None, {"a": 1}, ["acp"], [], {}]
KEYS = ["surface", "working_dir", "k", "NaN", "a b"]


def check_contract(key: str, value: object) -> None:
    if not all(" " <= ch <= "~" for ch in key) or UNSUPPORTED_KEY_CHARS & set(key):
        raise ValueError(f"unsupported metadata filter key: {key!r}")
    if not isinstance(value, str):
        raise TypeError(f"metadata filter values must be strings, not {type(value).__name__}")
    if value[:1] in ("{", "[") or NUL in value:
        raise ValueError(f"unsupported metadata filter value: {value[:20]!r}")


def python_match(document: str, filters: list[tuple[str, str]]) -> bool:
    metadata = parse_json_object(document)
    for key, value in filters:
        stored = metadata.get(key)
        if not isinstance(stored, str) or stored != value:
            return False
    return True


def other_clauses(target: str | None) -> tuple[list[str], list[object]]:
    if target:
        return ["target = ?"], [target]
    return [], []


def two_tier(store, *, router: str, limit: int, newest_first: bool, target, metadata_filter, shape: str = "subquery"):
    backend = store._backend
    direction = "DESC" if newest_first else "ASC"
    clauses, params = other_clauses(target)
    filters: list[tuple[str, str]] = []
    conditions: list[str] = []
    condition_params: list[object] = []
    for key, value in metadata_filter.items():
        normalized = str(key or "").strip()
        if not normalized:
            continue
        value = value or ""
        check_contract(normalized, value)
        filters.append((normalized, value))
        conditions.append(
            "(CAST(metadata_json AS jsonb) -> CAST(? AS text))"
            " = to_jsonb(CAST(? AS text))"
        )
        condition_params.extend([normalized, value])
    where = " AND ".join(clauses) or "TRUE"
    if router == "valid":
        route_sql, route_params = "pg_input_is_valid(metadata_json, 'jsonb')", []
    else:
        route_sql, route_params = "metadata_json !~ ?", [ROUTE_PATTERN]
    matched = " AND ".join(conditions)
    if shape == "cte":
        sql = f"""
            WITH candidates AS (
                SELECT {SESSION_COLUMNS}, {route_sql} AS metadata_in_sql
                FROM sessions
                WHERE {where}
            )
            SELECT {SESSION_COLUMNS}, metadata_in_sql
            FROM candidates
            WHERE CASE WHEN metadata_in_sql THEN {matched} ELSE TRUE END
            ORDER BY updated_at {direction}, id {direction}
            LIMIT ? + (SELECT count(*) FROM candidates WHERE NOT metadata_in_sql)
        """
        all_params = [*route_params, *params, *condition_params, limit]
    elif shape == "subquery":
        sql = f"""
            SELECT {SESSION_COLUMNS}, {route_sql} AS metadata_in_sql
            FROM sessions
            WHERE {where} AND CASE WHEN {route_sql} THEN {matched} ELSE TRUE END
            ORDER BY updated_at {direction}, id {direction}
            LIMIT ? + (SELECT count(*) FROM sessions WHERE {where} AND NOT ({route_sql}))
        """
        all_params = [
            *route_params, *params, *route_params, *condition_params, limit, *params, *route_params,
        ]
    else:  # window
        sql = f"""
            SELECT {SESSION_COLUMNS}, metadata_in_sql FROM (
                SELECT {SESSION_COLUMNS}, metadata_in_sql,
                       row_number() OVER (ORDER BY updated_at {direction}, id {direction}) AS position,
                       count(*) FILTER (WHERE NOT metadata_in_sql) OVER () AS unparsed
                FROM (
                    SELECT {SESSION_COLUMNS}, {route_sql} AS metadata_in_sql
                    FROM sessions WHERE {where} OFFSET 0
                ) AS candidates
                WHERE CASE WHEN metadata_in_sql THEN {matched} ELSE TRUE END
            ) AS ranked
            WHERE position <= ? + unparsed
            ORDER BY position
        """
        all_params = [*route_params, *params, *condition_params, limit]
    rows = backend.query_dicts(sql, all_params)
    selected, routed = [], 0
    for row in rows:
        if not row.pop("metadata_in_sql"):
            routed += 1
            if not python_match(str(row["metadata_json"]), filters):
                continue
        selected.append(row)
        if len(selected) == limit:
            break
    return [row_to_session(row) for row in selected], len(rows), routed


def python_pages(store, *, limit: int, newest_first: bool, target, metadata_filter):
    backend = store._backend
    direction = "DESC" if newest_first else "ASC"
    compare = "<" if newest_first else ">"
    filters = [
        (str(k).strip(), v or "") for k, v in metadata_filter.items() if str(k or "").strip()
    ]
    clauses, params = other_clauses(target)
    selected, fetched, cursor = [], 0, None
    page = max(limit, 100)
    while len(selected) < limit:
        where = list(clauses)
        page_params = list(params)
        if cursor is not None:
            where.append(f"(updated_at, id) {compare} (?, ?)")
            page_params.extend(cursor)
        rows = backend.query_dicts(
            f"SELECT {SESSION_COLUMNS} FROM sessions WHERE {' AND '.join(where) or 'TRUE'} "
            f"ORDER BY updated_at {direction}, id {direction} LIMIT ?",
            [*page_params, page],
        )
        fetched += len(rows)
        for row in rows:
            if python_match(str(row["metadata_json"]), filters):
                selected.append(row)
                if len(selected) == limit:
                    break
        if len(rows) < page:
            break
        cursor = (rows[-1]["updated_at"], rows[-1]["id"])
    return [row_to_session(row) for row in selected], fetched, None


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
        self._schema = f"om406_{uuid.uuid4().hex[:12]}"
        with self._admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{self._schema}"'))
        self.sqlite = build_runtime_storage(
            Path(self._tmp.name) / "s" / "openminion.db", env={}
        )
        self.postgres = build_runtime_storage(
            Path(self._tmp.name) / "p" / "openminion.db",
            env={},
            record_backend="record.postgres",
            record_backend_options={"url": schema_url(base, self._schema)},
        )

    def close(self) -> None:
        self.sqlite.close()
        self.postgres.close()
        with self._admin.begin() as conn:
            conn.execute(self._sa.text(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE'))
        self._admin.dispose()
        self._tmp.cleanup()


def random_metadata(rng: random.Random, odd_rate: float) -> dict:
    metadata: dict = {}
    for key in rng.sample(KEYS + ["other", "n"], rng.randint(0, 4)):
        roll = rng.random()
        if roll < 0.6:
            metadata[key] = rng.choice(STRINGS)
        else:
            metadata[key] = rng.choice(OTHER_VALUES)
    if rng.random() < odd_rate:
        metadata[rng.choice(["n", "other", "k", "surface"])] = rng.choice(ODD)
    return metadata


def insert_rows(stores: Stores, rows: list[tuple]) -> None:
    sql = (
        "INSERT INTO sessions(id, session_key, channel, target, metadata_json, "
        "created_at, updated_at, status, last_activity_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    for storage in (stores.sqlite, stores.postgres):
        record_store = storage.record_store
        with record_store.transaction():
            for row in rows:
                record_store.execute_count(sql, row)


def build_corpus(stores: Stores, count: int, seed: int, odd_rate: float) -> list[tuple]:
    rng = random.Random(seed)
    stamps = [
        f"2026-10-25T{10 + minute // 60:02d}:{minute % 60:02d}:00+00:00"
        for minute in range(max(5, count // 20))
    ]
    rows = []
    for index in range(count):
        session_id = f"s-{rng.getrandbits(48):012x}-{index:06d}"
        stamp = rng.choice(stamps)
        rows.append(
            (
                session_id,
                f"key-{session_id}",
                "console",
                rng.choice(["t1", "t2", "t3"]),
                metadata_json(random_metadata(rng, odd_rate)),
                stamp,
                stamp,
                "active",
                stamp,
            )
        )
    insert_rows(stores, rows)
    return rows


def random_filter(rng: random.Random) -> dict:
    keys = rng.sample(KEYS, rng.choice([1, 1, 1, 2]))
    pool = [s for s in STRINGS if s[:1] not in ("{", "[") and NUL not in s]
    return {key: rng.choice(pool) for key in keys}


def differential(count: int, filters: int, seed: int) -> None:
    stores = Stores()
    try:
        build_corpus(stores, count, seed, odd_rate=0.05)
        documents = [
            row["metadata_json"]
            for row in stores.postgres.record_store.query_dicts("SELECT metadata_json FROM sessions")
        ]
        routers = os.environ.get("PROTO_ROUTERS", "valid,pattern,python").split(",")
        version = stores.postgres.record_store.query_dicts("SHOW server_version")[0]["server_version"]
        rejects = None
        if "valid" in routers:
            rejects = stores.postgres.record_store.query_dicts(
                "SELECT count(*) AS n FROM sessions WHERE NOT pg_input_is_valid(metadata_json, 'jsonb')"
            )[0]["n"]
        print(json.dumps({"postgres": version, "rows": len(documents), "jsonb_rejects": rejects, "seed": seed}))
        try:
            stores.postgres.sessions._backend.query_dicts(
                "SELECT count(*) FROM sessions WHERE (CAST(metadata_json AS jsonb) -> 'k') IS NOT NULL"
            )
            print("whole-document jsonb cast: no error")
        except Exception as exc:  # noqa: BLE001
            print("whole-document jsonb cast:", type(exc).__name__, str(exc).splitlines()[0])
        rng = random.Random(seed + 1)
        mismatches = {name: 0 for name in routers}
        routed_total = {name: 0 for name in routers if name != "python"}
        nonempty = 0
        for _ in range(filters):
            metadata_filter = random_filter(rng)
            limit = rng.choice([1, 2, 3, 5, 10, 50, 100000])
            newest_first = rng.random() < 0.7
            target = rng.choice([None, None, "t1", "t2"])
            expected = [
                s.id
                for s in stores.sqlite.sessions.list_sessions(
                    limit=limit,
                    newest_first=newest_first,
                    target=target,
                    metadata_filter=metadata_filter,
                )
            ]
            nonempty += bool(expected)
            for name, fn in (
                ("valid", lambda **kw: two_tier(router="valid", **kw)),
                ("pattern", lambda **kw: two_tier(router="pattern", **kw)),
                ("python", python_pages),
            ):
                if name not in routers:
                    continue
                got, _, routed = fn(
                    store=stores.postgres.sessions,
                    limit=limit,
                    newest_first=newest_first,
                    target=target,
                    metadata_filter=metadata_filter,
                )
                if routed is not None:
                    routed_total[name] += routed
                ids = [s.id for s in got]
                if ids != expected:
                    mismatches[name] += 1
                    if mismatches[name] <= 5:
                        print(
                            "MISMATCH",
                            name,
                            ascii(metadata_filter),
                            limit,
                            newest_first,
                            target,
                            "sqlite",
                            expected[:5],
                            "postgres",
                            ids[:5],
                        )
        print(
            json.dumps(
                {
                    "filters": filters,
                    "nonempty_results": nonempty,
                    "mismatches": mismatches,
                    "rows_routed_to_python": routed_total,
                    "sqlite": sqlite3.sqlite_version,
                }
            )
        )
    finally:
        stores.close()


def realistic_metadata(rng: random.Random, index: int) -> dict:
    surface = rng.choices(["acp", "cli", "gateway", "focus"], weights=[5, 60, 25, 10])[0]
    project = f"/home/user/projects/p{rng.randrange(200):03d}"
    metadata = {
        "surface": surface,
        "workspace_root": project,
        "title": f"session {index} about topic {rng.randrange(1000)}",
        "turns": rng.randrange(50),
    }
    if surface == "focus":
        metadata.update({"working_dir": project, "focus_mode": True, "cwd": project})
    return metadata


def head_sql(store, *, limit: int, metadata_filter: dict):
    """This head's predicate: the whole document cast to jsonb."""
    where = " AND ".join(
        "(CAST(metadata_json AS jsonb) -> CAST(? AS text)) = to_jsonb(CAST(? AS text))"
        for _ in metadata_filter
    )
    params: list[object] = []
    for key, value in metadata_filter.items():
        params.extend([key, value])
    rows = store._backend.query_dicts(
        f"SELECT {SESSION_COLUMNS} FROM sessions WHERE {where} "
        "ORDER BY updated_at DESC, id DESC LIMIT ?",
        [*params, limit],
    )
    return [row_to_session(row) for row in rows], len(rows), None


def realistic_rows(count: int) -> list[tuple]:
    rng = random.Random(11)
    stamps = [
        f"2026-10-{1 + day:02d}T{hour:02d}:{minute:02d}:00+00:00"
        for day in range(25) for hour in range(24) for minute in range(0, 60, 7)
    ]
    rows = []
    for index in range(count):
        session_id = f"s-{rng.getrandbits(48):012x}-{index:06d}"
        stamp = rng.choice(stamps)
        rows.append(
            (session_id, f"key-{session_id}", "console", "t1",
             metadata_json(realistic_metadata(rng, index)), stamp, stamp, "active", stamp)
        )
    return rows


def add_odd_documents(record_store, rows: list[tuple], count: int) -> None:
    odd_rng = random.Random(5)
    for row in odd_rng.sample(rows, count):
        metadata = parse_json_object(row[4])
        metadata["note"] = odd_rng.choice(ODD)
        record_store.execute_count(
            "UPDATE sessions SET metadata_json = ? WHERE id = ?", (metadata_json(metadata), row[0])
        )


def shapes(count: int) -> None:
    """Statement shapes for (a), on the 0.5% corpus."""
    stores = Stores()
    try:
        rows = realistic_rows(count)
        insert_rows(stores, rows)
        record_store = stores.postgres.record_store
        add_odd_documents(record_store, rows, count // 200)
        record_store.execute_count("ANALYZE sessions")

        def median_ms(fn, repeat: int = 9) -> float:
            samples = []
            for _ in range(repeat):
                start = time.perf_counter()
                fn()
                samples.append((time.perf_counter() - start) * 1000)
            return statistics.median(samples)

        probes = {
            "count(*)": "SELECT count(*) FROM sessions",
            "count(*) WHERE pg_input_is_valid": (
                "SELECT count(*) FROM sessions WHERE pg_input_is_valid(metadata_json, 'jsonb')"
            ),
            "count(*) WHERE jsonb -> surface, parseable rows": (
                "SELECT count(*) FROM sessions WHERE CASE WHEN pg_input_is_valid(metadata_json, 'jsonb') "
                "THEN (CAST(metadata_json AS jsonb) -> 'surface') IS NOT NULL END"
            ),
        }
        for label, sql in probes.items():
            print(f"probe | {label} | median {median_ms(lambda: record_store.query_dicts(sql)):.1f} ms")
        cases = {
            "surface=acp, limit 100": {"surface": "acp"},
            "surface=cli (60%), limit 100": {"surface": "cli"},
            "working_dir (rare), limit 100": {"working_dir": "/home/user/projects/p007"},
        }
        for label, metadata_filter in cases.items():
            reference = None
            for shape in ("subquery", "cte", "window"):
                def call(shape=shape):
                    return two_tier(
                        router="valid", shape=shape, store=stores.postgres.sessions, limit=100,
                        newest_first=True, target=None, metadata_filter=metadata_filter,
                    )
                got, fetched, routed = call()
                ids = [s.id for s in got]
                if reference is None:
                    reference = ids
                print(
                    f"{label} | (a) {shape} | median {median_ms(call):.1f} ms | rows fetched {fetched}"
                    f" | returned {len(ids)} | same ids as subquery: {ids == reference}"
                )
    finally:
        stores.close()


def timing(count: int) -> None:
    stores = Stores()
    try:
        rng = random.Random(11)
        stamps = [
            f"2026-10-{1 + day:02d}T{hour:02d}:{minute:02d}:00+00:00"
            for day in range(25) for hour in range(24) for minute in range(0, 60, 7)
        ]
        rows = []
        for index in range(count):
            session_id = f"s-{rng.getrandbits(48):012x}-{index:06d}"
            stamp = rng.choice(stamps)
            rows.append(
                (session_id, f"key-{session_id}", "console", "t1",
                 metadata_json(realistic_metadata(rng, index)), stamp, stamp, "active", stamp)
            )
        insert_rows(stores, rows)
        record_store = stores.postgres.record_store
        cases = {
            "surface=acp, limit 100": {"surface": "acp"},
            "working_dir (25 matches), limit 100": {"working_dir": "/home/user/projects/p007"},
        }
        candidates = (
            ("this head (whole-document jsonb cast)", head_sql),
            ("(a) two-tier, pg_input_is_valid", lambda **kw: two_tier(router="valid", **kw)),
            ("(b) two-tier, pattern", lambda **kw: two_tier(router="pattern", **kw)),
            ("(c) Python, keyset pages", python_pages),
        )

        def measure(phase: str) -> None:
            record_store.execute_count("ANALYZE sessions")
            odd = record_store.query_dicts(
                "SELECT count(*) AS n FROM sessions WHERE NOT pg_input_is_valid(metadata_json, 'jsonb')"
            )[0]["n"]
            pattern = record_store.query_dicts(
                "SELECT count(*) AS n FROM sessions WHERE metadata_json ~ ?", [ROUTE_PATTERN]
            )[0]["n"]
            print(f"## {phase}: {count} sessions, {odd} that jsonb rejects, {pattern} the pattern routes")
            for label, metadata_filter in cases.items():
                for name, fn in candidates:
                    samples, fetched, got = [], 0, []
                    try:
                        for _ in range(9):
                            start = time.perf_counter()
                            if fn is head_sql:
                                got, fetched, _ = fn(store=stores.postgres.sessions, limit=100, metadata_filter=metadata_filter)
                            else:
                                got, fetched, _ = fn(
                                    store=stores.postgres.sessions, limit=100, newest_first=True,
                                    target=None, metadata_filter=metadata_filter,
                                )
                            samples.append((time.perf_counter() - start) * 1000)
                    except Exception as exc:  # noqa: BLE001
                        print(f"{label} | {name} | {type(exc).__name__}: {str(exc).splitlines()[0][:90]}")
                        continue
                    print(
                        f"{label} | {name} | median {statistics.median(samples):.1f} ms"
                        f" | rows fetched {fetched} | returned {len(got)}"
                    )

        measure("no stored document jsonb rejects")
        odd_rng = random.Random(5)
        for row in odd_rng.sample(rows, count // 200):
            metadata = parse_json_object(row[4])
            metadata["note"] = odd_rng.choice(ODD)
            record_store.execute_count(
                "UPDATE sessions SET metadata_json = ? WHERE id = ?", (metadata_json(metadata), row[0])
            )
        measure("0.5% of the documents hold NaN, Infinity, NUL or a lone surrogate")
    finally:
        stores.close()


def construction() -> None:
    """Direct SessionStore construction on a PostgreSQL record store."""
    import sqlalchemy as sa

    from openminion.modules.storage.backends.postgres import RecordStorePostgres
    from openminion.modules.storage.record_store import RecordStoreSQLite
    from openminion.modules.storage.runtime.migrations import migrate_record_store
    from openminion.modules.storage.runtime.session_store import SessionStore

    base = os.environ["OPENMINION_TEST_POSTGRES_URL"]
    admin = sa.create_engine(base, future=True)
    schema = f"om406_{uuid.uuid4().hex[:12]}"
    with admin.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    record_store = RecordStorePostgres(schema_url(base, schema))
    try:
        migrate_record_store(record_store, backend_type="postgres")
        store = SessionStore(record_store)
        print("diagnostics backend:", record_store.diagnostics().get("backend"))
        print("isinstance RecordStorePostgres:", isinstance(record_store, RecordStorePostgres))
        print("isinstance RecordStoreSQLite:", isinstance(record_store, RecordStoreSQLite))
        try:
            store.list_sessions(metadata_filter={"surface": "acp"})
            print("direct SessionStore(RecordStorePostgres) filter: ok")
        except Exception as exc:  # noqa: BLE001
            print("direct SessionStore(RecordStorePostgres) filter:", type(exc).__name__, str(exc).splitlines()[0])
    finally:
        record_store.close()
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "differential":
        differential(
            int(sys.argv[2]) if len(sys.argv) > 2 else 3000,
            int(sys.argv[3]) if len(sys.argv) > 3 else 400,
            int(sys.argv[4]) if len(sys.argv) > 4 else 1,
        )
    elif mode == "timing":
        timing(int(sys.argv[2]) if len(sys.argv) > 2 else 50000)
    elif mode == "shapes":
        shapes(int(sys.argv[2]) if len(sys.argv) > 2 else 50000)
    else:
        construction()
