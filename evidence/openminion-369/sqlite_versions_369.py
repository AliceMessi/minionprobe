"""The dev SQLite metadata filter SQL on this interpreter's SQLite (openminion#369).

Runs `json_extract(metadata_json, '$.' || key) = value` exactly as
`list_sessions(metadata_filter=...)` binds it on dev, on metadata written by
`json.dumps(..., sort_keys=True)`, without importing openminion.
"""

import json
import sqlite3

CASES = [
    ("string", {"k": "x"}, "k", "x"),
    ("object vs minified text", {"k": {"a": 1}}, "k", '{"a":1}'),
    ("dotted key, nested", {"a": {"b": "x"}}, "a.b", "x"),
    ("dotted key, top-level", {"a.b": "x"}, "a.b", "x"),
    ("quote inside key", {'a"b': "x"}, 'a"b', "x"),
    ("backslash in key", {"a\\b": "x"}, "a\\b", "x"),
    ("non-ASCII key", {"caf\u00e9": "x"}, "caf\u00e9", "x"),
    ("control char in key", {"a\tb": "x"}, "a\tb", "x"),
    ("trailing dot key", {"a.": "x"}, "a.", "x"),
    ("NaN in another key", {"k": "x", "n": float("nan")}, "k", "x"),
    ("Infinity in another key", {"k": "x", "n": float("inf")}, "k", "x"),
    ("NUL in another key", {"k": "x", "n": "a\x00b"}, "k", "x"),
    ("unpaired surrogate in another key", {"k": "x", "n": "\ud800"}, "k", "x"),
]

conn = sqlite3.connect(":memory:")
print("sqlite", sqlite3.sqlite_version)
for label, metadata, key, value in CASES:
    document = json.dumps(metadata, sort_keys=True)
    try:
        row = conn.execute(
            "SELECT json_extract(?, ?) = ?", (document, f"$.{key}", value)
        ).fetchone()
        outcome = "match" if row[0] == 1 else "no match"
    except sqlite3.Error as exc:
        outcome = f"error {type(exc).__name__}: {exc}"
    print(f"{label}: {outcome}")
