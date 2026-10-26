"""Helpers for the #364 installed-package probes (room participant join order).

Only the installed openminion is imported; nothing comes from the repo's tests.
The tied-clock technique is the issue's: the participant clock is patched to one
timestamp and the participant row ids to values that sort in reverse join order.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid as _uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Sequence
from unittest.mock import patch
from urllib.parse import quote_plus

PARTICIPANT_CLOCK = "openminion.modules.storage.runtime.session_store.participants.utc_now_iso"
PARTICIPANT_UUID = "openminion.modules.storage.runtime.session_store.participants.uuid4"
EARLIER_AT = "2026-10-22T00:59:00+00:00"
TIED_AT = "2026-10-22T01:00:00+00:00"
LATER_AT = "2026-10-22T01:01:00+00:00"

# The order list_participants read before runtime migration 14.
PRE_UPGRADE_ORDER_SQL = (
    "SELECT participant_id FROM room_participants "
    "WHERE session_id = ? AND left_at IS NULL ORDER BY joined_at ASC, id ASC"
)
# An independent numbering: each row counts the rows of its session at or
# before it in (joined_at, id) order (the order migration 14 must keep).
COUNTED_JOIN_SEQ_SQL = """
SELECT participant.id AS id, (
    SELECT COUNT(*)
    FROM room_participants AS earlier
    WHERE earlier.session_id = participant.session_id
      AND (earlier.joined_at < participant.joined_at
           OR (earlier.joined_at = participant.joined_at AND earlier.id <= participant.id))
) AS join_seq
FROM room_participants AS participant
"""
ROW_COLUMNS = (
    "id, session_id, participant_type, participant_id, channel, role, "
    "display_name, joined_at, left_at"
)


def rid(scope: str, char: str) -> str:
    """A row id that starts with `char`; the rest is fixed per room (scope), so
    ids of one room sort by their first character and stay unique across rooms."""
    return char + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:31]


def row_ids(scope: str, chars: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(hex=rid(scope, char)) for char in chars]


def id_feed(values: Sequence[str]) -> Callable[[], SimpleNamespace]:
    """uuid4 replacement: the given hex ids in turn, then real random ids."""
    pending = list(values)

    def _next() -> SimpleNamespace:
        if pending:
            return SimpleNamespace(hex=pending.pop(0))
        return SimpleNamespace(hex=_uuid.uuid4().hex)

    return _next


def participant_ids(sessions: Any, session_id: str) -> list[str]:
    return [item.participant_id for item in sessions.list_participants(session_id)]


def join_seq_by_participant(sessions: Any, session_id: str) -> dict[str, int]:
    rows = sessions._backend.query_dicts(
        "SELECT participant_id, join_seq FROM room_participants WHERE session_id = ?",
        (session_id,),
    )
    return {str(row["participant_id"]): int(row["join_seq"]) for row in rows}


def row_of(sessions: Any, session_id: str, participant_id: str) -> dict[str, Any]:
    rows = sessions._backend.query_dicts(
        f"SELECT {ROW_COLUMNS} FROM room_participants WHERE session_id = ? AND participant_id = ?",
        (session_id, participant_id),
    )
    return dict(rows[0]) if rows else {}


def join_agents(
    sessions: Any,
    session_id: str,
    agents: Sequence[str],
    *,
    ids: str | Sequence[str],
    at: str = TIED_AT,
) -> list[Any]:
    hexes = [rid(session_id, c) for c in ids] if isinstance(ids, str) else list(ids)
    with patch(PARTICIPANT_CLOCK, return_value=at), patch(PARTICIPANT_UUID, side_effect=id_feed(hexes)):
        return [
            sessions.add_participant(session_id=session_id, participant_type="agent", participant_id=agent)
            for agent in agents
        ]


def remove_agent(sessions: Any, session_id: str, agent: str, *, at: str = TIED_AT) -> bool:
    with patch(PARTICIPANT_CLOCK, return_value=at):
        return sessions.remove_participant(session_id=session_id, participant_type="agent", participant_id=agent)


def rows_digest(rows: list[dict[str, Any]]) -> str:
    norm = sorted(json.dumps([None if r.get(k) is None else str(r.get(k)) for k in ROW_COLUMNS.split(", ")]) for r in rows)
    return hashlib.sha256("\n".join(norm).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ backends
def pg_base_url() -> str:
    return os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()


def schema_url(base: str, schema: str) -> str:
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}options={quote_plus(f'-csearch_path={schema}')}"


def pg_create_schema(base: str, schema: str) -> None:
    import sqlalchemy as sa

    admin = sa.create_engine(base, future=True)
    try:
        with admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    finally:
        admin.dispose()


def pg_drop_schema(base: str, schema: str) -> None:
    import sqlalchemy as sa

    admin = sa.create_engine(base, future=True)
    try:
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        admin.dispose()


@contextmanager
def pg_schema(prefix: str) -> Iterator[str]:
    base = pg_base_url()
    schema = f"{prefix}_{_uuid.uuid4().hex[:12]}"
    pg_create_schema(base, schema)
    try:
        yield schema_url(base, schema)
    finally:
        pg_drop_schema(base, schema)


def pg_server_version(url: str) -> str:
    import sqlalchemy as sa

    engine = sa.create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            return str(conn.execute(sa.text("select version()")).scalar())
    finally:
        engine.dispose()


def open_storage(db_path: Path, url: str = ""):
    """build_runtime_storage on SQLite (db_path) or on PostgreSQL (url)."""
    from openminion.modules.storage.runtime.context import build_runtime_storage

    if url:
        return build_runtime_storage(
            db_path, env={}, record_backend="record.postgres", record_backend_options={"url": url}
        )
    return build_runtime_storage(db_path, env={})


class Raw:
    """Direct database access outside openminion (stdlib sqlite3 or SQLAlchemy)."""

    def __init__(self, db_path: Path, url: str = "") -> None:
        self.db_path = Path(db_path)
        self.url = url

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        if self.url:
            import sqlalchemy as sa

            engine = sa.create_engine(self.url, future=True)
            try:
                with engine.connect() as conn:
                    names = {f"p{i}": v for i, v in enumerate(params)}
                    text = sql
                    for i in range(len(params)):
                        text = text.replace("?", f":p{i}", 1)
                    result = conn.execute(sa.text(text), names)
                    return [dict(row._mapping) for row in result] if result.returns_rows else []
            finally:
                engine.dispose()
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(sql, tuple(params))
            if cur.description is None:
                return []
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def execute_script(self, statements: Sequence[str]) -> None:
        if self.url:
            import sqlalchemy as sa

            engine = sa.create_engine(self.url, future=True)
            try:
                with engine.begin() as conn:
                    for statement in statements:
                        conn.exec_driver_sql(statement)
            finally:
                engine.dispose()
            return
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        try:
            for statement in statements:
                conn.execute(statement)
            conn.commit()
        finally:
            conn.close()

    def ledger(self) -> list[dict[str, Any]]:
        return [
            {"version": int(r["version"]), "name": str(r["name"]), "applied_at": str(r["applied_at"])}
            for r in self.query("SELECT version, name, applied_at FROM migrations ORDER BY version")
        ]

    def columns(self, table: str) -> list[str]:
        if self.url:
            rows = self.query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ? ORDER BY ordinal_position",
                (table,),
            )
            return [str(r["column_name"]) for r in rows]
        return [str(r["name"]) for r in self.query(f"PRAGMA table_info({table})")]

    def column_info(self, table: str, column: str) -> dict[str, Any]:
        if self.url:
            rows = self.query(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?",
                (table, column),
            )
            return dict(rows[0]) if rows else {}
        for r in self.query(f"PRAGMA table_info({table})"):
            if r["name"] == column:
                return {"type": r["type"], "notnull": r["notnull"], "dflt_value": r["dflt_value"]}
        return {}

    def index(self, name: str) -> dict[str, Any]:
        if self.url:
            rows = self.query(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = ?",
                (name,),
            )
            return dict(rows[0]) if rows else {}
        rows = self.query("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,))
        if not rows:
            return {}
        info = dict(rows[0])
        unique = [r for r in self.query("PRAGMA index_list(room_participants)") if r["name"] == name]
        info["unique"] = bool(unique and unique[0]["unique"])
        info["columns"] = [r["name"] for r in self.query(f"PRAGMA index_info({name})")]
        return info

    def tables(self) -> list[str]:
        if self.url:
            rows = self.query(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() ORDER BY 1"
            )
            return [str(r["table_name"]) for r in rows]
        return [str(r["name"]) for r in self.query("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1")]

    def participant_rows(self) -> list[dict[str, Any]]:
        return self.query(f"SELECT {ROW_COLUMNS} FROM room_participants")
