"""PostgreSQL facts for the openminion#406 proposal.

Each stored document is built the way `metadata_json()` writes session
metadata: `json.dumps(metadata, sort_keys=True)` with the defaults
(`ensure_ascii=True`, `allow_nan=True`). For each document it reports what
PostgreSQL does with the `json` and `jsonb` casts, `->`, `->>`,
`json_typeof` and `pg_input_is_valid`, each in its own autocommit statement.

    python pg_facts_406.py postgresql://postgres:test@localhost:5432/postgres
"""

from __future__ import annotations

import json
import sys

import psycopg

NUL = chr(0)
HI = chr(0xD800)
LO = chr(0xDC00)

DOCUMENTS = {
    "plain": {"k": "x"},
    "NUL in another value": {"k": "x", "n": "a" + NUL + "b"},
    "NUL in the filtered value": {"k": "a" + NUL + "b"},
    "lone high surrogate in another value": {"k": "x", "n": HI},
    "lone low surrogate in another value": {"k": "x", "n": LO},
    "lone surrogate in the filtered value": {"k": HI},
    "surrogate pair (emoji) in another value": {"k": "x", "n": chr(0x1F600)},
    "NaN in another value": {"k": "x", "n": float("nan")},
    "Infinity in another value": {"k": "x", "n": float("inf")},
    "-Infinity in another value": {"k": "x", "n": float("-inf")},
    "NaN as the filtered value": {"k": float("nan")},
    "non-ASCII in another value": {"k": "x", "n": "caf" + chr(0xE9)},
    "CJK in another value": {"k": "x", "n": chr(0x4E2D)},
    "NUL in another key": {"k": "x", "a" + NUL + "b": 1},
    "NaN-looking string": {"k": "x", "n": "trace: NaN"},
    "object as the filtered value": {"k": {"b": 1, "a": [1, "x"]}},
}

PROBES = {
    "json cast": "SELECT CAST(%(d)s AS json) IS NOT NULL",
    "jsonb cast": "SELECT CAST(%(d)s AS jsonb) IS NOT NULL",
    "json -> k (text)": "SELECT (CAST(%(d)s AS json) -> 'k')::text",
    "json ->> k": "SELECT CAST(%(d)s AS json) ->> 'k'",
    "json_typeof(json -> k)": "SELECT json_typeof(CAST(%(d)s AS json) -> 'k')",
    "jsonb -> k": "SELECT (CAST(%(d)s AS jsonb) -> 'k')::text",
    "pg_input_is_valid json": "SELECT pg_input_is_valid(%(d)s, 'json')",
    "pg_input_is_valid jsonb": "SELECT pg_input_is_valid(%(d)s, 'jsonb')",
    "CASE guard": (
        "SELECT CASE WHEN pg_input_is_valid(%(d)s, 'jsonb') "
        "THEN (CAST(%(d)s AS jsonb) -> 'k') = to_jsonb(CAST('x' AS text)) "
        "ELSE NULL END"
    ),
}


def run(conn: psycopg.Connection, sql: str, params: dict) -> str:
    try:
        value = conn.execute(sql, params).fetchone()[0]
        return repr(value)
    except Exception as exc:  # noqa: BLE001
        message = str(exc).splitlines()[0]
        return f"ERROR {type(exc).__name__}: {message}"


def execute(conn: psycopg.Connection, sql: str) -> str:
    try:
        conn.execute(sql)
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR {type(exc).__name__}: {str(exc).splitlines()[0]}"


def main() -> None:
    url = sys.argv[1]
    with psycopg.connect(url, autocommit=True) as conn:
        print("version:", conn.execute("SELECT version()").fetchone()[0])
        print("server_version_num:", conn.execute("SHOW server_version_num").fetchone()[0])
        print("server_encoding:", conn.execute("SHOW server_encoding").fetchone()[0])
        documents = {label: json.dumps(m, sort_keys=True) for label, m in DOCUMENTS.items()}
        documents["duplicate member k"] = json.dumps(
            {float("nan"): "first", float("nan"): "x"}, sort_keys=True
        ).replace('"NaN"', '"k"')
        for label, document in documents.items():
            print(f"\n## {label}: {ascii(document)}")
            for probe, sql in PROBES.items():
                print(f"  {probe}: {run(conn, sql, {'d': document})}")
        print("\n## CASE guard over a table column (not a bound constant)")
        execute(conn, "CREATE TEMP TABLE docs (label text, d text)")
        for label, document in documents.items():
            conn.execute("INSERT INTO docs VALUES (%(l)s, %(d)s)", {"l": label, "d": document})
        guard = (
            "SELECT label, CASE WHEN pg_input_is_valid(d, 'jsonb') "
            "THEN (CAST(d AS jsonb) -> 'k') = to_jsonb(CAST('x' AS text)) ELSE NULL END "
            "FROM docs ORDER BY label"
        )
        try:
            for label, value in conn.execute(guard).fetchall():
                print(f"  {label}: {value!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {type(exc).__name__}: {str(exc).splitlines()[0]}")
        print("\n## LIMIT with a scalar subquery over a CTE")
        print(
            "  ",
            run(
                conn,
                "WITH c AS (SELECT g FROM generate_series(1, 10) AS g) "
                "SELECT count(*) FROM (SELECT g FROM c ORDER BY g "
                "LIMIT %(n)s + (SELECT count(*) FROM c WHERE g > 7)) AS t",
                {"n": 2},
            ),
        )
        for encoding in ("SQL_ASCII", "LATIN1"):
            name = f"facts_{encoding.lower()}"
            execute(conn, f"DROP DATABASE IF EXISTS {name}")
            created = execute(
                conn,
                f"CREATE DATABASE {name} ENCODING '{encoding}' TEMPLATE template0 "
                "LC_COLLATE 'C' LC_CTYPE 'C'",
            )
            print(f"\n## database encoding {encoding}: create {created}")
            if created.startswith("ERROR"):
                continue
            other = url.rsplit("/", 1)[0] + "/" + name
            with psycopg.connect(other, autocommit=True) as conn2:
                for label in ("plain", "non-ASCII in another value", "CJK in another value"):
                    document = json.dumps(DOCUMENTS[label], sort_keys=True)
                    jsonb = run(conn2, PROBES["jsonb cast"], {"d": document})
                    json_arrow = run(conn2, PROBES["json -> k (text)"], {"d": document})
                    valid = run(conn2, PROBES["pg_input_is_valid jsonb"], {"d": document})
                    print(
                        f"  {label}: jsonb cast {jsonb}; json -> k {json_arrow};"
                        f" pg_input_is_valid jsonb {valid}"
                    )
            execute(conn, f"DROP DATABASE IF EXISTS {name}")


if __name__ == "__main__":
    main()
