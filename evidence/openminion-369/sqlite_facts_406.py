"""SQLite facts for the openminion#406 proposal (standard library only).

Runs the reader `list_sessions(metadata_filter=...)` uses on SQLite,
`json_extract(metadata_json, '$.' || key) = value`, on documents written as
`metadata_json()` writes them: `json.dumps(metadata, sort_keys=True)`.

    python -I sqlite_facts_406.py
"""

from __future__ import annotations

import json
import sqlite3

NUL = chr(0)
BS = chr(92)
NAN = float("nan")


def doc(metadata: object) -> str:
    return json.dumps(metadata, sort_keys=True)


def extract(conn: sqlite3.Connection, document: str, key: str) -> str:
    try:
        row = conn.execute(
            "SELECT json_extract(?, ?), typeof(json_extract(?, ?))",
            (document, "$." + key, document, "$." + key),
        ).fetchone()
        return f"{row[0]!r} ({row[1]})"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR {type(exc).__name__}: {exc}"


def matches(conn: sqlite3.Connection, document: str, key: str, value: object) -> str:
    try:
        row = conn.execute(
            "SELECT json_extract(?, ?) = ?", (document, "$." + key, value)
        ).fetchone()
        return repr(row[0])
    except Exception as exc:  # noqa: BLE001
        return f"ERROR {type(exc).__name__}: {exc}"


KEY_CASES = [
    # (label, metadata, key)
    ("dotted key, literal and nested both stored", {"a.b": "lit", "a": {"b": "nested"}}, "a.b"),
    ("dotted key, literal only", {"a.b": "lit"}, "a.b"),
    ("bracketed key, literal and array both stored", {"a[0]": "lit", "a": ["idx"]}, "a[0]"),
    ("quoted key", {'"a"': "lit", "a": "plain"}, '"a"'),
    ("quoted key with a dot inside", {"a.b": "lit"}, '"a.b"'),
    ("key ending in a dot", {"a.": "lit", "a": "plain"}, "a."),
    ("key with an unclosed bracket", {"a[": "lit"}, "a["),
    ("key with a bracket and text", {"a[x": "lit"}, "a[x"),
    ("key with a closing bracket", {"a]": "lit"}, "a]"),
    ("key with an inner quote", {'a"b': "lit"}, 'a"b'),
    ("key with a leading quote, unclosed", {'"ab': "lit"}, '"ab'),
    ("key with a backslash", {"a" + BS + "b": "lit"}, "a" + BS + "b"),
    ("non-ASCII key", {"caf" + chr(0xE9): "lit"}, "caf" + chr(0xE9)),
    ("key with a newline", {"a\nb": "lit"}, "a\nb"),
    ("key $", {"$": "lit"}, "$"),
    ("key *", {"*": "lit"}, "*"),
    ("key #", {"#": "lit"}, "#"),
    ("key with a space", {"a b": "lit"}, "a b"),
    ("key -1", {"-1": "lit"}, "-1"),
    ("key with NUL, prefix stored", {"k": "prefix", "k" + NUL + "z": "lit"}, "k" + NUL + "z"),
    ("plain key", {"surface": "acp"}, "surface"),
]

VALUE_CASES = [
    ("object", {"k": {"b": 1, "a": [1, 2.5, "x", None, True]}}),
    ("object with escaped strings", {"k": {"s": 'q"b' + BS + "c" + chr(0xE9) + "\n"}}),
    ("array with NaN and Infinity", {"k": [NAN, float("inf"), float("-inf")]}),
    ("empty object", {"k": {}}),
    ("float 1e20 and 1.0", {"k": [1e20, 1.0, 10**20]}),
]


def main() -> None:
    conn = sqlite3.connect(":memory:")
    print("sqlite", sqlite3.sqlite_version)
    print("\n## keys: json_extract(doc, '$.' || key)")
    for label, metadata, key in KEY_CASES:
        document = doc(metadata)
        print(f"  {label}: key {ascii(key)} doc {ascii(document)} -> {ascii(extract(conn, document, key))}")
    print("\n## object and array values: json_extract(doc, '$.k')")
    for label, metadata in VALUE_CASES:
        document = doc(metadata)
        print(f"  {label}: doc {ascii(document)} -> {ascii(extract(conn, document, 'k'))}")
    print("\n## other documents: json_extract(doc, '$.k') = 'x'")
    others = {
        "NaN in another value": doc({"k": "x", "n": NAN}),
        "NUL in another value": doc({"k": "x", "n": "a" + NUL + "b"}),
        "lone surrogate in another value": doc({"k": "x", "n": chr(0xD800)}),
        "duplicate member k in the text": '{"k": "first", "k": "x"}',
    }
    for label, document in others.items():
        print(f"  {label}: {ascii(document)} -> {matches(conn, document, 'k', 'x')}")
    nan_keys = {NAN: "first", float("nan"): "second"}
    document = doc(nan_keys)
    print(f"  two float NaN keys: {ascii(document)} -> $.NaN {ascii(extract(conn, document, 'NaN'))}; json.loads {json.loads(document)!r}")
    print("\n## filter values")
    print("  NUL in the value:", matches(conn, doc({"k": "a" + NUL + "b"}), "k", "a" + NUL + "b"))
    try:
        print("  lone surrogate in the value:", matches(conn, doc({"k": "x"}), "k", chr(0xD800)))
    except Exception as exc:  # noqa: BLE001
        print(f"  lone surrogate in the value: ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
