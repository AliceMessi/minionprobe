"""Session metadata filter differential for openminion#369.

Each case stores one session through the runtime session store, with the
metadata as `SessionStore` writes it (`json.dumps(..., sort_keys=True)`), and
calls `list_sessions(target=<case>, metadata_filter=<filter>)` on a fresh
store. It prints one JSON line per case and backend with the outcome:
`match`, `no match` or the error. The `python` field is the in-memory
reference `metadata.get(key.strip()) == (value or "")` over every non-empty key.

    PYTHONPATH=src python differential_369.py sqlite [postgres]

PostgreSQL needs OPENMINION_TEST_POSTGRES_URL; each case uses its own schema.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote_plus

from openminion.modules.storage.runtime.context import build_runtime_storage

NAN = float("nan")
INF = float("inf")

CASES: list[tuple[str, str, dict, dict]] = [
    # (group, case, metadata, filter)
    ("string", "equal", {"k": "acp"}, {"k": "acp"}),
    ("string", "other case", {"k": "ACP"}, {"k": "acp"}),
    ("string", "trailing space", {"k": "acp "}, {"k": "acp"}),
    ("string", "empty string", {"k": ""}, {"k": ""}),
    ("string", "None filter value (sent as '')", {"k": ""}, {"k": None}),
    ("string", "non-ASCII", {"k": "caf\u00e9"}, {"k": "caf\u00e9"}),
    ("string", "emoji (surrogate pair)", {"k": "\U0001f600"}, {"k": "\U0001f600"}),
    ("string", "quote and backslash", {"k": 'a"b\\c'}, {"k": 'a"b\\c'}),
    ("string", "newline and tab", {"k": "a\nb\tc"}, {"k": "a\nb\tc"}),
    ("string", "U+2028", {"k": "a\u2028b"}, {"k": "a\u2028b"}),
    ("string", "looks like a number", {"k": "1"}, {"k": "1"}),
    ("string", "looks like true", {"k": "true"}, {"k": "true"}),
    ("string", "looks like null", {"k": "null"}, {"k": "null"}),
    ("string", "looks like an object", {"k": '{"a":1}'}, {"k": '{"a":1}'}),
    ("missing", "missing key", {"other": "acp"}, {"k": "acp"}),
    ("missing", "empty metadata, filter ''", {}, {"k": ""}),
    ("missing", "missing key, filter None", {"other": "x"}, {"k": None}),
    ("null", "null vs ''", {"k": None}, {"k": ""}),
    ("null", "null vs 'null'", {"k": None}, {"k": "null"}),
    ("null", "null vs 'None'", {"k": None}, {"k": "None"}),
    ("null", "null vs None filter", {"k": None}, {"k": None}),
    ("number", "1 vs '1'", {"k": 1}, {"k": "1"}),
    ("number", "1.5 vs '1.5'", {"k": 1.5}, {"k": "1.5"}),
    ("number", "1.0 vs '1.0'", {"k": 1.0}, {"k": "1.0"}),
    ("number", "1.0 vs '1'", {"k": 1.0}, {"k": "1"}),
    ("number", "0 vs ''", {"k": 0}, {"k": ""}),
    ("number", "10**20 vs text", {"k": 10**20}, {"k": "100000000000000000000"}),
    ("boolean", "true vs 'true'", {"k": True}, {"k": "true"}),
    ("boolean", "true vs '1'", {"k": True}, {"k": "1"}),
    ("boolean", "true vs 'True'", {"k": True}, {"k": "True"}),
    ("boolean", "false vs 'false'", {"k": False}, {"k": "false"}),
    ("boolean", "false vs '0'", {"k": False}, {"k": "0"}),
    ("boolean", "false vs ''", {"k": False}, {"k": ""}),
    ("object", "object vs minified text", {"k": {"a": 1}}, {"k": '{"a":1}'}),
    ("object", "object vs json.dumps text", {"k": {"a": 1}}, {"k": '{"a": 1}'}),
    ("object", "empty object vs '{}'", {"k": {}}, {"k": "{}"}),
    ("object", "array vs minified text", {"k": [1, "x"]}, {"k": '[1,"x"]'}),
    ("object", "array vs json.dumps text", {"k": [1, "x"]}, {"k": '[1, "x"]'}),
    ("object", "array of one string vs the string", {"k": ["x"]}, {"k": "x"}),
    ("multiple", "both equal", {"k": "x", "j": "y"}, {"k": "x", "j": "y"}),
    ("multiple", "second differs", {"k": "x", "j": "y"}, {"k": "x", "j": "z"}),
    ("multiple", "second missing", {"k": "x"}, {"k": "x", "j": "y"}),
    ("multiple", "with an empty key", {"k": "x"}, {"k": "x", " ": "y"}),
    ("key", "surrounding spaces (stripped)", {"k": "x"}, {" k ": "x"}),
    ("key", "empty key (skipped)", {"k": "x"}, {"": "zzz"}),
    ("key", "dotted key, top-level 'a.b'", {"a.b": "x"}, {"a.b": "x"}),
    ("key", "dotted key, nested a -> b", {"a": {"b": "x"}}, {"a.b": "x"}),
    ("key", "bracket key, top-level 'a[0]'", {"a[0]": "x"}, {"a[0]": "x"}),
    ("key", "bracket key, array a[0]", {"a": ["x"]}, {"a[0]": "x"}),
    ("key", "quote inside key 'a\"b'", {'a"b': "x"}, {'a"b': "x"}),
    ("key", "quoted key '\"a\"', top-level '\"a\"'", {'"a"': "x"}, {'"a"': "x"}),
    ("key", "quoted key '\"a\"', top-level 'a'", {"a": "x"}, {'"a"': "x"}),
    ("key", "'$k'", {"$k": "x"}, {"$k": "x"}),
    ("key", "'$'", {"$": "x"}, {"$": "x"}),
    ("key", "space inside 'a b'", {"a b": "x"}, {"a b": "x"}),
    ("key", "single quote 'a'b'", {"a'b": "x"}, {"a'b": "x"}),
    ("key", "backslash 'a\\b'", {"a\\b": "x"}, {"a\\b": "x"}),
    ("key", "LIKE wildcards '%_'", {"%_": "x"}, {"%_": "x"}),
    ("key", "wildcard '*'", {"*": "x"}, {"*": "x"}),
    ("key", "non-ASCII 'caf\u00e9'", {"caf\u00e9": "x"}, {"caf\u00e9": "x"}),
    ("key", "SQL text key", {"k') OR ('1'='1": "x"}, {"k') OR ('1'='1": "x"}),
    ("key", "SQL text key vs other row", {"k": "x"}, {"k') OR ('1'='1": "x"}),
    ("key", "trailing dot 'a.'", {"a.": "x"}, {"a.": "x"}),
    ("key", "digits '0'", {"0": "x"}, {"0": "x"}),
    ("unreadable", "NaN in another key", {"k": "x", "n": NAN}, {"k": "x"}),
    ("unreadable", "Infinity in another key", {"k": "x", "n": INF}, {"k": "x"}),
    ("unreadable", "-Infinity in another key", {"k": "x", "n": -INF}, {"k": "x"}),
    ("unreadable", "NUL in another key", {"k": "x", "n": "a\x00b"}, {"k": "x"}),
    ("unreadable", "NUL in the value", {"k": "a\x00b"}, {"k": "a\x00b"}),
    ("unreadable", "NUL in the filter only", {"k": "x"}, {"k": "x\x00"}),
    ("unreadable", "unpaired surrogate in another key", {"k": "x", "n": "\ud800"}, {"k": "x"}),
    ("outside str API", "int filter 1 vs 1", {"k": 1}, {"k": 1}),
    ("outside str API", "int filter 1 vs '1'", {"k": "1"}, {"k": 1}),
    ("outside str API", "int filter 1 vs true", {"k": True}, {"k": 1}),
    ("outside str API", "True filter vs true", {"k": True}, {"k": True}),
    ("outside str API", "True filter vs 'true'", {"k": "true"}, {"k": True}),
]


def _reference(metadata: dict, metadata_filter: dict) -> str:
    for key, value in metadata_filter.items():
        normalized = str(key or "").strip()
        if not normalized:
            continue
        if metadata.get(normalized, object()) != (value or ""):
            return "no match"
    return "match"


def _schema_url(base_url: str, schema_name: str) -> str:
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}options={quote_plus(f'-csearch_path={schema_name}')}"


def _run_case(backend: str, index: int, metadata: dict, metadata_filter: dict) -> dict:
    target = f"case-{index}"
    with tempfile.TemporaryDirectory() as tmp:
        options: dict = {}
        record_backend = "record.sqlite"
        admin = schema = None
        if backend == "postgres":
            import sqlalchemy as sa

            base = os.environ["OPENMINION_TEST_POSTGRES_URL"]
            admin = sa.create_engine(base, future=True)
            schema = f"om369_{uuid.uuid4().hex[:12]}"
            with admin.begin() as conn:
                conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            record_backend = "record.postgres"
            options = {"url": _schema_url(base, schema)}
        storage = build_runtime_storage(
            Path(tmp) / "state" / "openminion.db",
            env={},
            record_backend=record_backend,
            record_backend_options=options,
        )
        try:
            session = storage.sessions.resolve_session(
                agent_id="main",
                channel="console",
                target=target,
                metadata=metadata,
            )
            stored = storage.record_store.query_dicts(
                "SELECT metadata_json FROM sessions WHERE id = ?", (session.id,)
            )[0]["metadata_json"]
            try:
                rows = storage.sessions.list_sessions(
                    limit=10, target=target, metadata_filter=metadata_filter
                )
                ids = [row.id for row in rows]
                if ids == [session.id]:
                    outcome = "match"
                elif not ids:
                    outcome = "no match"
                else:
                    outcome = f"unexpected {ids}"
            except Exception as exc:  # noqa: BLE001 - the outcome is the error
                outcome = _error(exc)
            naive = None
            if backend == "postgres":
                naive = _naive_text_filter(storage, session.id, metadata_filter)
        finally:
            storage.close()
            if admin is not None:
                import sqlalchemy as sa

                with admin.begin() as conn:
                    conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                admin.dispose()
    result = {"stored": stored, "outcome": outcome}
    if naive is not None:
        result["naive_text"] = naive
    return result


def _error(exc: Exception) -> str:
    text = str(exc).splitlines()[0] if str(exc) else ""
    return f"error {type(exc).__name__}: {text[:120]}"


def _naive_text_filter(storage, session_id: str, metadata_filter: dict) -> str:
    """The same filter with the text operator `->>`, for comparison only."""
    clauses, params = [], [session_id]
    for key, value in metadata_filter.items():
        normalized = str(key or "").strip()
        if not normalized:
            continue
        clauses.append("(CAST(metadata_json AS jsonb) ->> CAST(? AS text)) = ?")
        params.extend([normalized, value or ""])
    where = " AND ".join(["id = ?", *clauses])
    try:
        rows = storage.record_store.query_dicts(
            f"SELECT id FROM sessions WHERE {where}", params
        )
    except Exception as exc:  # noqa: BLE001 - the outcome is the error
        return _error(exc)
    return "match" if rows else "no match"


def main(backends: list[str]) -> None:
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "sqlite": sqlite3.sqlite_version,
                "openminion": __import__("openminion").__file__,
            }
        ),
        flush=True,
    )
    for backend in backends:
        for index, (group, case, metadata, metadata_filter) in enumerate(CASES):
            result = _run_case(backend, index, metadata, metadata_filter)
            print(
                json.dumps(
                    {
                        "backend": backend,
                        "group": group,
                        "case": case,
                        "filter": {k: v for k, v in metadata_filter.items()},
                        "python": _reference(metadata, metadata_filter),
                        **result,
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main(sys.argv[1:] or ["sqlite"])
