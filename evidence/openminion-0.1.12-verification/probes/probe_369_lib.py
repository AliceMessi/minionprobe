"""Shared data and helpers for the #369 installed-package probes.

Imports only the installed openminion (0.1.11 or 0.1.12) and the standard
library (plus SQLAlchemy, a dependency of openminion[postgres], for the
PostgreSQL schema handling). Nothing here comes from the repo's tests; the
filter tables restate the contract in the 0.1.12 CHANGELOG and
docs/runtime-database-upgrades.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import queue
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote_plus

import openminion

VERSION = getattr(openminion, "__version__", "")
IS_0112 = VERSION == "0.1.12"

NUL = chr(0)
NAN = float("nan")
INF = float("inf")
LONE_HIGH = chr(0xD800)
LONE_LOW = chr(0xDC00)
CAFE = "caf" + chr(0xE9)
EMOJI = chr(0x1F600)
LONG_KEY = "k" * 5_000
LONG_VALUE = "v" * 100_000

# ------------------------------------------------------------------ filter matrix
# Session id -> metadata. The filters below select the same sessions on both
# backends (top-level string members only).
FILTER_SESSIONS: dict[str, dict[str, Any]] = {
    "acp-a": {"surface": "acp", "workspace_root": "/work/a"},
    "acp-b": {"surface": "acp", "workspace_root": "/work/b"},
    "cli-a": {"surface": "cli", "workspace_root": "/work/a"},
    "no-surface": {"workspace_root": "/work/a"},
    "null-surface": {"surface": None},
    "empty-surface": {"surface": ""},
    "list-surface": {"surface": ["acp"]},
    "int-value": {"value": 1},
    "float-value": {"value": 1.5},
    "true-value": {"value": True},
    "false-value": {"value": False},
    "null-value": {"value": None},
    "object-value": {"value": {"kind": "acp"}},
    "text-one": {"value": "1"},
    "text-true": {"value": "true"},
    "text-null": {"value": "null"},
    "quote-key": {"it's": "yes"},
    "like-key": {"100%_done": "yes"},
    "dollar-key": {"$scope": "yes"},
    "space-key": {"a b": "yes"},
    "sql-key": {"k') OR ('1'='1": "yes"},
    "escaped-value": {"note": 'say "hi" \\ C:\\work\\a'},
    "unicode-value": {"note": CAFE + " " + EMOJI + " " + chr(0x2028)},
    "newline-value": {"note": "line\nbreak\ttab"},
    "like-value": {"note": "a%"},
}

FILTER_EXPECTED: list[tuple[dict[str, Any], set[str]]] = [
    ({"surface": "acp"}, {"acp-a", "acp-b"}),
    ({"surface": "focus"}, set()),
    ({"surface": "ACP"}, set()),
    ({"surface": "acp", "workspace_root": "/work/a"}, {"acp-a"}),
    ({"surface": "acp", "workspace_root": "/work/c"}, set()),
    ({"surface": "acp", "missing": "acp"}, set()),
    ({"missing": "acp"}, set()),
    ({"missing": ""}, set()),
    ({"surface": ""}, {"empty-surface"}),
    ({"surface": None}, {"empty-surface"}),
    ({"  surface  ": "cli"}, {"cli-a"}),
    ({"": "ignored", "surface": "cli"}, {"cli-a"}),
    ({"value": "1"}, {"text-one"}),
    ({"value": "1.5"}, set()),
    ({"value": "true"}, {"text-true"}),
    ({"value": "True"}, set()),
    ({"value": "false"}, set()),
    ({"value": "0"}, set()),
    ({"value": "null"}, {"text-null"}),
    ({"value": "None"}, set()),
    ({"value": ""}, set()),
    ({"value": '{"kind": "acp"}'}, set()),
    ({"it's": "yes"}, {"quote-key"}),
    ({"100%_done": "yes"}, {"like-key"}),
    ({"100%": "yes"}, set()),
    ({"$scope": "yes"}, {"dollar-key"}),
    ({"a b": "yes"}, {"space-key"}),
    ({"k') OR ('1'='1": "yes"}, {"sql-key"}),
    ({"k') OR ('1'='1": "no"}, set()),
    ({"note": 'say "hi" \\ C:\\work\\a'}, {"escaped-value"}),
    ({"note": CAFE + " " + EMOJI + " " + chr(0x2028)}, {"unicode-value"}),
    ({"note": "line\nbreak\ttab"}, {"newline-value"}),
    ({"note": "a%"}, {"like-value"}),
    ({"note": "a_"}, set()),
]

# Where the backends read stored metadata differently (documented in 0.1.12):
# filter, SQLite ids, PostgreSQL ids.
DIFF_SESSIONS: dict[str, dict[str, Any]] = {
    "object": {"value": {"kind": "acp"}},
    "array": {"value": [1, "x"]},
    "dotted-top": {"a.b": "top"},
    "dotted-nested": {"a": {"b": "nested"}},
    "bracket-top": {"tags[0]": "top"},
    "bracket-array": {"tags": ["first"]},
    "quoted-top": {'"q"': "top"},
    "quoted-plain": {"q": "plain"},
}
DIFF_FILTERS: list[tuple[dict[str, Any], set[str], set[str]]] = [
    ({"value": '{"kind":"acp"}'}, {"object"}, set()),
    ({"value": '[1,"x"]'}, {"array"}, set()),
    ({"a.b": "top"}, set(), {"dotted-top"}),
    ({"a.b": "nested"}, {"dotted-nested"}, set()),
    ({"tags[0]": "top"}, set(), {"bracket-top"}),
    ({"tags[0]": "first"}, {"bracket-array"}, set()),
    ({'"q"': "top"}, set(), {"quoted-top"}),
    ({'"q"': "plain"}, {"quoted-plain"}, set()),
]

# Metadata PostgreSQL cannot parse as a whole document, or that a filter must
# read literally.
ODD_METADATA: dict[str, dict[Any, Any]] = {
    "odd-nan-other": {"surface": "acp", "score": NAN},
    "odd-inf-other": {"surface": "acp", "low": -INF, "high": INF},
    "odd-nul-other": {"surface": "acp", "note": "a" + NUL + "b"},
    "odd-surrogate-other": {"surface": "acp", "note": LONE_HIGH + "x" + LONE_LOW},
    "odd-nul-value": {"surface": "a" + NUL + "b"},
    "odd-nul-key": {"k" + NUL + "z": "value"},
    "odd-surrogate-value": {"surface": "x" + LONE_HIGH},
    "odd-unicode": {CAFE: EMOJI, "surface": CAFE},
    "odd-brace-value": {"surface": '{"a":1}', "working_dir": "[draft]"},
    "odd-object-value": {"surface": {"a": 1}, "working_dir": ["draft"]},
    "odd-path-keys": {"a.b": "literal", "a": {"b": "nested"}, "t[0]": "x", '"q"': "y"},
    "odd-long": {LONG_KEY: LONG_VALUE, "surface": "acp"},
    "odd-nan-keys": {NAN: "first", float("nan"): "second"},
    "odd-non-strings": {"surface": 1, "flag": True, "none": None, "n": 1.5},
    "odd-empty-string": {"surface": ""},
}
POSTGRES_ODD_EXPECTED: list[tuple[dict[str, str], set[str]]] = [
    ({"surface": "acp"}, {"odd-nan-other", "odd-inf-other", "odd-nul-other", "odd-surrogate-other", "odd-long"}),
    ({"surface": "a" + NUL + "b"}, {"odd-nul-value"}),
    ({"k" + NUL + "z": "value"}, {"odd-nul-key"}),
    ({"surface": "x" + LONE_HIGH}, {"odd-surrogate-value"}),
    ({"note": LONE_HIGH + "x" + LONE_LOW}, {"odd-surrogate-other"}),
    ({CAFE: EMOJI}, {"odd-unicode"}),
    ({"surface": CAFE}, {"odd-unicode"}),
    ({"surface": '{"a":1}'}, {"odd-brace-value"}),
    ({"working_dir": "[draft]"}, {"odd-brace-value"}),
    ({"working_dir": '["draft"]'}, set()),
    ({"a.b": "literal"}, {"odd-path-keys"}),
    ({"a.b": "nested"}, set()),
    ({"t[0]": "x"}, {"odd-path-keys"}),
    ({'"q"': "y"}, {"odd-path-keys"}),
    ({LONG_KEY: LONG_VALUE}, {"odd-long"}),
    ({LONG_KEY: LONG_VALUE[:-1]}, set()),
    ({"NaN": "second"}, {"odd-nan-keys"}),
    ({"NaN": "first"}, set()),
    ({"surface": "1"}, set()),
    ({"flag": "true"}, set()),
    ({"none": ""}, set()),
    ({"surface": ""}, {"odd-empty-string"}),
    ({"surface": "acp", "note": "a" + NUL + "b"}, {"odd-nul-other"}),
]


# Filters over the 1,200 bulk sessions of probe_369_seed.py (target "legacy").
LEGACY_FILTERS: list[dict[str, str]] = [
    {"surface": "acp"},
    {"surface": "cli", "workspace_root": "/work/3"},
    {"title": "legacy session 150"},
    {"tag": "s" + LONE_HIGH},
    {"note": "n" + NUL + "ul"},
    {"surface": "missing"},
]


def show_filter(item: dict[Any, Any]) -> str:
    """A filter as printable ASCII (long members shortened)."""
    shown = {}
    for key, value in item.items():
        k = key if len(str(key)) <= 40 else f"<{len(key)} x {key[0]!r}>"
        v = value if not isinstance(value, str) or len(value) <= 40 else f"<{len(value)} x {value[0]!r}>"
        shown[k] = v
    return ascii(shown)


# ------------------------------------------------------------------ storage
def pg_admin_engine(base_url: str):
    import sqlalchemy as sa

    return sa.create_engine(base_url)


def schema_url(base_url: str, schema: str) -> str:
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}options={quote_plus(f'-csearch_path={schema}')}"


def create_schema(base_url: str, schema: str) -> None:
    import sqlalchemy as sa

    engine = pg_admin_engine(base_url)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    finally:
        engine.dispose()


def drop_schema(base_url: str, schema: str) -> None:
    import sqlalchemy as sa

    engine = pg_admin_engine(base_url)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        engine.dispose()


@contextmanager
def pg_schema(base_url: str, schema: str, *, keep: bool = False) -> Iterator[str]:
    create_schema(base_url, schema)
    try:
        yield schema_url(base_url, schema)
    finally:
        if not keep:
            drop_schema(base_url, schema)


def pg_server_version(base_url: str) -> str:
    import sqlalchemy as sa

    engine = pg_admin_engine(base_url)
    try:
        with engine.connect() as conn:
            return str(conn.execute(sa.text("select version()")).scalar())
    finally:
        engine.dispose()


def open_storage(backend: str, *, db_path: Path | None = None, url: str | None = None, work: Path | None = None):
    """The runtime storage the runtime itself builds (migrations included)."""
    from openminion.modules.storage.runtime.context import build_runtime_storage

    if backend == "sqlite":
        assert db_path is not None
        return build_runtime_storage(db_path, env={})
    assert url is not None and work is not None
    return build_runtime_storage(
        work / "unused-sqlite-path.db",
        env={},
        record_backend="record.postgres",
        record_backend_options={"url": url},
    )


def backend_of(storage) -> Any:
    return storage.sessions._backend  # RuntimeSessionStoreBackend


def q(storage, sql: str, params: Any = ()) -> list[dict[str, Any]]:
    return backend_of(storage).query_dicts(sql, list(params))


def ledger(storage) -> list[tuple[int, str]]:
    return [(int(r["version"]), str(r["name"])) for r in q(storage, "SELECT version, name FROM migrations ORDER BY version")]


def table_exists(storage, backend: str, table: str) -> bool:
    if backend == "sqlite":
        rows = q(storage, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", [table])
    else:
        rows = q(storage, "SELECT table_name AS name FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = ?", [table])
    return bool(rows)


def index_names(storage, backend: str, table: str) -> list[str]:
    if backend == "sqlite":
        rows = q(storage, "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND name NOT LIKE 'sqlite_autoindex%' ORDER BY name", [table])
    else:
        rows = q(storage, "SELECT indexname AS name FROM pg_indexes WHERE schemaname = current_schema() AND tablename = ? ORDER BY indexname", [table])
    return [str(r["name"]) for r in rows]


def stored_metadata(storage) -> dict[str, str]:
    return {str(r["id"]): str(r["metadata_json"]) for r in q(storage, "SELECT id, metadata_json FROM sessions ORDER BY id")}


def metadata_digest(meta: dict[str, str]) -> str:
    h = hashlib.sha256()
    for sid in sorted(meta):
        h.update(sid.encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
        h.update(meta[sid].encode("utf-8", "surrogatepass"))
        h.update(b"\x01")
    return h.hexdigest()


def stored_rows(storage) -> set[tuple[str, str, str, str]]:
    return {
        (str(r["session_id"]), str(r["key"]), str(r["value"]), str(r["entry_digest"]))
        for r in q(storage, "SELECT session_id, key, value, entry_digest FROM session_metadata_strings")
    }


def expected_rows(meta: dict[str, str]) -> set[tuple[str, str, str, str]]:
    """The rows the 0.1.12 contract derives from stored metadata, computed here.

    `json.loads` of each stored `metadata_json`; one row per top-level member
    whose value is a str; name and value stored as `json.dumps` text; digest =
    first 16 hex of sha256(name_text + value_text).
    """
    out: set[tuple[str, str, str, str]] = set()
    for sid, raw in meta.items():
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            continue
        for name, value in parsed.items():
            if isinstance(value, str):
                k, v = json.dumps(name), json.dumps(value)
                out.add((sid, k, v, hashlib.sha256((k + v).encode("ascii")).hexdigest()[:16]))
    return out


def store_sessions(sessions, metadata_by_id: dict[str, dict[Any, Any]], target: str) -> None:
    for session_id, metadata in metadata_by_id.items():
        sessions.resolve_session(agent_id="main", channel="console", target=target, session_id=session_id, metadata=metadata)


def selected(sessions, flt: dict[Any, Any], target: str, limit: int = 1000, newest_first: bool = True) -> list[str]:
    rows = sessions.list_sessions(limit=limit, newest_first=newest_first, target=target, metadata_filter=flt)
    return [row.id for row in rows]


def outcome_ids(sessions, flt: dict[Any, Any], target: str) -> Any:
    try:
        return sorted(selected(sessions, flt, target))
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}"


def reference_ids(storage, flt: dict[Any, Any], target: str, *, limit: int, newest_first: bool) -> list[str]:
    """The ids the PostgreSQL contract selects, from stored rows, in Python."""
    rows = q(storage, "SELECT id, metadata_json, updated_at FROM sessions WHERE target = ?", [target])
    conds = []
    for key, value in flt.items():
        name = str(key or "").strip()
        if name:
            conds.append((name, value or ""))
    hits = []
    for r in rows:
        meta = json.loads(str(r["metadata_json"]))
        meta = meta if isinstance(meta, dict) else {}
        if all(isinstance(meta.get(n), str) and meta.get(n) == v for n, v in conds):
            hits.append((str(r["updated_at"]), str(r["id"])))
    hits.sort(reverse=newest_first)
    return [sid for _, sid in hits[:limit]]


# ------------------------------------------------------------------ runtime config
def write_config(root: Path, *, pg_url: str | None = None, agents: tuple[str, ...] = ("openminion",)) -> Path:
    """Echo-provider config with explicit roots; PostgreSQL runtime storage when pg_url."""
    from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config

    config = OpenMinionConfig()
    config.agents = {
        name: AgentProfileConfig(name=name, provider="echo", default_channel="console") for name in agents
    }
    config.default_agent = agents[0]
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / ".openminion" / "state.db")
    if pg_url:
        config.storage.backend = "postgres"
        config.storage.postgres_url = pg_url
    root.mkdir(parents=True, exist_ok=True)
    path = root / "agents.json"
    save_config(config, str(path))
    return path


def api_runtime(root: Path, *, pg_url: str | None = None, agents: tuple[str, ...] = ("openminion",)):
    from openminion import APIRuntime

    path = write_config(root, pg_url=pg_url, agents=agents)
    return APIRuntime.from_config_path(str(path), home_root=root, data_root=root / ".openminion")


def cli_exe() -> str:
    exe = shutil.which("openminion", path=str(Path(sys.executable).parent))
    if not exe:
        raise RuntimeError("installed openminion console script not found next to the interpreter")
    return exe


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("OPENMINION_")}


class AcpProcess:
    """`openminion --config ... acp` as a subprocess, spoken to over stdio (ACP v1 JSON-RPC)."""

    def __init__(self, root: Path, config: Path, stderr_path: Path) -> None:
        self._stderr = open(stderr_path, "wb")
        self.proc = subprocess.Popen(
            [cli_exe(), "--config", str(config), "--home-root", str(root), "--data-root", str(root / ".openminion"), "acp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr, env=clean_env(), cwd=str(root),
        )
        self._lines: "queue.Queue[bytes]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._next = 0
        self.notifications: list[dict[str, Any]] = []

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)

    def call(self, method: str, params: dict[str, Any], timeout: float = 180) -> dict[str, Any]:
        self._next += 1
        ident = self._next
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}) + "\n").encode())
        self.proc.stdin.flush()
        while True:
            msg = json.loads(self._lines.get(timeout=timeout))
            if msg.get("id") == ident and ("result" in msg or "error" in msg):
                return msg
            self.notifications.append(msg)

    def close(self) -> int:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            return self.proc.wait(timeout=120)
        finally:
            self._stderr.close()
