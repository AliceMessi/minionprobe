"""Helpers for probe_393.py: runtime storages on one SQLite file or one
PostgreSQL schema, holds at the join's activation seams, an active-agent write
log, and independent (non-openminion) reads of the stored rows.

The holds do not change what a call does: a wrapper around the participant
owner's session read (or the backend's execute_count) waits at the seam, then
returns the real result, or raises an injected exception where a case says so.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import patch
from urllib.parse import quote

from _common import sanitize_text

PARTICIPANTS = "openminion.modules.storage.runtime.session_store.participants"
ACTIVATION_WRITE = "UPDATE sessions SET active_agent_id"
ROW_WRITE = "INSERT INTO room_participants"
HOLD_SECONDS = 1.5  # below the 5 s SQLite busy timeout of the runtime storages
HOLD_TIMEOUT = 30
OP = threading.local()


def mask(text: Any) -> str:
    return sanitize_text(re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1***@", str(text)))


def describe(exc: BaseException) -> str:
    return mask(f"{type(exc).__module__}.{type(exc).__name__}: {exc}".splitlines()[0])[:300]


# ----------------------------------------------------------------- write log
class WriteLog:
    """Every statement that writes sessions.active_agent_id, from any storage, in order."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def mark(self) -> int:
        return len(self.entries)

    def since(self, mark: int, session_id: str | None = None) -> list[dict[str, Any]]:
        items = self.entries[mark:]
        if session_id is not None:
            items = [e for e in items if e["session"] == session_id]
        return items

    @staticmethod
    def brief(items: list[dict[str, Any]]) -> list[list[Any]]:
        return [[e["by"], e["op"], e["value"]] for e in items]


def instrument(storage: Any, role: str, log: WriteLog) -> Any:
    """Record each active-agent write this storage runs: who, in which probe
    operation, the value, and whether it ran inside an open transaction."""
    backend = storage.sessions._backend
    original = backend.execute_count

    def execute_count(sql: str, params: Any = None) -> int:
        result = original(sql, params)
        if sql.lstrip().startswith(ACTIVATION_WRITE):
            values = list(params or [])
            with log.lock:
                log.entries.append({
                    "by": role,
                    "op": getattr(OP, "label", None) or threading.current_thread().name,
                    "value": values[0] if values else None,
                    "session": values[-1] if values else None,
                    "rowcount": result,
                    "in_transaction": bool(storage.record_store.in_transaction),
                    "t": time.monotonic(),
                })
        return result

    backend.execute_count = execute_count
    return storage


# ----------------------------------------------------------------- holds
class Hold:
    def __init__(self) -> None:
        self.read = threading.Event()
        self.release = threading.Event()
        self.seen: Any = "<not reached>"
        self.in_transaction: bool | None = None
        self.t_reached: float | None = None


@contextmanager
def held(storage: Any, *, seam: str = "activation-read", fail: BaseException | None = None) -> Iterator[Hold]:
    """Hold the next join on `storage` at a seam until `release` is set.

    activation-read: the first session read after the join writes its
      participant row, the read that decides whether the join activates its
      agent (the seam of upstream's held_activation_read and of the report's
      probe). On 0.1.14 it runs after the participant transaction committed; on
      0.1.15 inside it, after the session-row lock.
    after-activation-write: right after the join's `UPDATE sessions SET
      active_agent_id` statement returns.
    With `fail`, the held call raises it when released instead of going on.
    """
    owner = storage.sessions._participants
    backend = owner._backend
    hold = Hold()
    wrote_row = threading.Event()
    execute_count = backend.execute_count
    get_session = owner._get_session

    def pause(record: Any) -> None:
        hold.seen = None if record is None else record.active_agent_id
        hold.in_transaction = bool(storage.record_store.in_transaction)
        hold.t_reached = time.monotonic()
        hold.read.set()
        if not hold.release.wait(timeout=HOLD_TIMEOUT):
            raise TimeoutError("held join was never released")
        if fail is not None:
            raise fail

    def execute_count_at_seam(sql: str, params: Any = None) -> int:
        count = execute_count(sql, params)
        if ROW_WRITE in sql:
            wrote_row.set()
        if seam == "after-activation-write" and sql.lstrip().startswith(ACTIVATION_WRITE) and not hold.read.is_set():
            pause(None)
        return count

    def get_session_at_seam(session_id: str) -> Any:
        record = get_session(session_id)
        if seam == "activation-read" and wrote_row.is_set() and not hold.read.is_set():
            pause(record)
        return record

    with patch.object(backend, "execute_count", execute_count_at_seam), patch.object(owner, "_get_session", get_session_at_seam):
        try:
            yield hold
        finally:
            hold.release.set()


class Op:
    """One probe operation in a pool thread, with its start/end times."""

    def __init__(self, pool: Any, label: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        self.label = label
        self.t_start = time.monotonic()
        self.t_end: float | None = None

        def run() -> Any:
            OP.label = label
            try:
                return fn(*args, **kwargs)
            finally:
                self.t_end = time.monotonic()
                OP.label = None

        self.future = pool.submit(run)

    def outcome(self, timeout: float = HOLD_TIMEOUT) -> str:
        try:
            value = self.future.result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - recorded
            return describe(exc)
        return f"ok: {value!r}"

    def seconds(self) -> float | None:
        return None if self.t_end is None else round(self.t_end - self.t_start, 3)


@contextmanager
def labelled(label: str) -> Iterator[None]:
    OP.label = label
    try:
        yield
    finally:
        OP.label = None


# ----------------------------------------------------------------- backends
class SqliteEnv:
    pg = False

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "state" / "openminion.db"

    def open(self, role: str, **_: Any) -> Any:
        from openminion.modules.storage.runtime.context import build_runtime_storage

        return build_runtime_storage(self.path, env={})

    def _q(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def describe_connections(self, storages: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for role, storage in storages.items():
            conn = storage.record_store.connection
            files = [r[2] for r in conn.execute("PRAGMA database_list").fetchall() if r[1] == "main"]
            out[role] = {
                "record_store": type(storage.record_store).__name__,
                "connection_id": id(conn),
                "file": mask(files[0] if files else None),
                "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                "busy_timeout_ms": conn.execute("PRAGMA busy_timeout").fetchone()[0],
            }
        return out

    def activity(self) -> list[dict[str, Any]]:
        return []

    def locks_held(self, role: str) -> int:
        return 0

    def drop(self) -> None:
        pass

    def raw_set_active(self, session_id: str, value: Any) -> None:
        """Write sessions.active_agent_id directly (sqlite3), as upstream's blank-value test does."""
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            with conn:
                conn.execute("UPDATE sessions SET active_agent_id = ? WHERE id = ?", (value, session_id))
        finally:
            conn.close()

    # independent reads (sqlite3, not openminion)
    def raw_room(self, session_id: str) -> dict[str, Any]:
        session = self._q("SELECT active_agent_id, session_key FROM sessions WHERE id = ?", (session_id,))
        rows = self._q(
            "SELECT participant_type, participant_id, role, joined_at, join_seq, left_at FROM room_participants "
            "WHERE session_id = ? ORDER BY joined_at, join_seq", (session_id,))
        return raw_view(session, rows)

    def raw_all(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        sessions = self._q("SELECT id, session_key, active_agent_id FROM sessions ORDER BY id")
        rows = self._q("SELECT session_id, participant_type, participant_id, left_at FROM room_participants")
        return sessions, rows


class PgEnv:
    pg = True

    def __init__(self, base: str, root: Path, tag: str = "p393") -> None:
        import sqlalchemy as sa

        self.sa = sa
        self.base = base
        self.root = root
        self.tag = tag
        self.schema = f"{tag}_{uuid.uuid4().hex[:12]}"
        self.admin = sa.create_engine(self._url("admin"), future=True)
        with self.admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{self.schema}"'))

    def _url(self, role: str, extra_options: str = "") -> str:
        options = f"-csearch_path={self.schema}" + (f" {extra_options}" if extra_options else "")
        sep = "&" if "?" in self.base else "?"
        return f"{self.base}{sep}options={quote(options, safe='')}&application_name={quote(self.app(role), safe='')}"

    def app(self, role: str) -> str:
        return f"{self.tag}-{role}"

    def open(self, role: str, *, lock_timeout_ms: int | None = None) -> Any:
        from openminion.modules.storage.runtime.context import build_runtime_storage

        extra = f"-clock_timeout={int(lock_timeout_ms)}" if lock_timeout_ms else ""
        return build_runtime_storage(
            self.root / "pg" / f"{role}.db",
            env={},
            record_backend="record.postgres",
            record_backend_options={"url": self._url(role, extra)},
        )

    def server(self) -> dict[str, Any]:
        with self.admin.connect() as conn:
            row = conn.execute(self.sa.text("SELECT version(), current_database(), current_setting('server_version')")).one()
        return {"version": row[0], "database": row[1], "server_version": row[2], "schema": self.schema}

    def describe_connections(self, storages: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for role, storage in storages.items():
            rs = storage.record_store
            row = rs.query_dicts(
                "SELECT current_schema() AS schema, current_setting('application_name') AS app, "
                "current_setting('search_path') AS search_path")[0]
            out[role] = {"record_store": type(rs).__name__, "engine_id": id(getattr(rs, "_engine", None)), **row}
        return out

    def activity(self) -> list[dict[str, Any]]:
        sql = (
            "SELECT pid, application_name AS app, state, wait_event_type, wait_event, "
            "left(regexp_replace(query, '\\s+', ' ', 'g'), 90) AS query, pg_blocking_pids(pid) AS blocked_by "
            "FROM pg_stat_activity WHERE application_name LIKE :p AND application_name <> :admin ORDER BY pid"
        )
        with self.admin.connect() as conn:
            rows = conn.execute(self.sa.text(sql), {"p": f"{self.tag}-%", "admin": self.app("admin")}).mappings().all()
        return [dict(r) | {"blocked_by": list(r["blocked_by"] or [])} for r in rows]

    def locks_held(self, role: str) -> int:
        sql = (
            "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
            "WHERE a.application_name = :app AND l.locktype IN ('relation', 'tuple', 'transactionid') AND l.granted"
            " AND (l.relation IS NULL OR l.relation NOT IN (SELECT oid FROM pg_class WHERE relnamespace = 'pg_catalog'::regnamespace))"
        )
        with self.admin.connect() as conn:
            return int(conn.execute(self.sa.text(sql), {"app": self.app(role)}).scalar() or 0)

    def connections(self) -> int:
        return len(self.activity())

    def _q(self, sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        with self.admin.connect() as conn:
            return [dict(r) for r in conn.execute(self.sa.text(sql), params or {}).mappings().all()]

    def raw_set_active(self, session_id: str, value: Any) -> None:
        """Write sessions.active_agent_id directly on the server, as upstream's blank-value test does."""
        with self.admin.begin() as conn:
            conn.execute(self.sa.text(f'UPDATE "{self.schema}".sessions SET active_agent_id = :v WHERE id = :id'),
                         {"v": value, "id": session_id})

    def raw_room(self, session_id: str) -> dict[str, Any]:
        s = f'"{self.schema}"'
        session = self._q(f"SELECT active_agent_id, session_key FROM {s}.sessions WHERE id = :id", {"id": session_id})
        rows = self._q(
            f"SELECT participant_type, participant_id, role, joined_at, join_seq, left_at FROM {s}.room_participants "
            "WHERE session_id = :id ORDER BY joined_at, join_seq", {"id": session_id})
        return raw_view(session, rows)

    def raw_all(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        s = f'"{self.schema}"'
        sessions = self._q(f"SELECT id, session_key, active_agent_id FROM {s}.sessions ORDER BY id")
        rows = self._q(f"SELECT session_id, participant_type, participant_id, left_at FROM {s}.room_participants")
        return sessions, rows

    def counts(self) -> dict[str, int]:
        s = f'"{self.schema}"'
        out = {}
        for table in ("sessions", "room_participants", "migrations"):
            out[table] = int(self._q(f"SELECT count(*) AS n FROM {s}.{table}")[0]["n"])
        return out

    def drop(self) -> None:
        with self.admin.begin() as conn:
            conn.execute(self.sa.text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
        self.admin.dispose()


def raw_view(session: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    active_rows = [r for r in rows if r["left_at"] is None]
    return {
        "exists": bool(session),
        "stored": session[0]["active_agent_id"] if session else "<no session>",
        "agents": [r["participant_id"] for r in active_rows if r["participant_type"] == "agent"],
        "participants": [[r["participant_type"], r["participant_id"], r["role"], r["join_seq"]] for r in active_rows],
        "left": [[r["participant_type"], r["participant_id"], r["join_seq"]] for r in rows if r["left_at"] is not None],
    }


def departed_or_unknown_active(sessions: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[list[Any]]:
    """Sessions whose stored active agent is set (not blank) but is not an active agent participant."""
    active = {(r["session_id"], r["participant_id"]) for r in rows
              if r["participant_type"] == "agent" and r["left_at"] is None}
    bad = []
    for s in sessions:
        value = (s["active_agent_id"] or "").strip()
        if value and (s["id"], value) not in active:
            bad.append([s["id"], s["active_agent_id"]])
    return bad


# ----------------------------------------------------------------- room helpers
def new_room(storage: Any, target: str, **kwargs: Any) -> str:
    """A room with its human owner, as `openminion room create` makes it."""
    room = storage.sessions.create_room(channel="cli", target=target, **kwargs)
    storage.sessions.add_participant(session_id=room.id, participant_type="human", participant_id="owner", role="owner")
    return room.id


def join(storage: Any, session_id: str, agent: str) -> str:
    return storage.sessions.add_participant(session_id=session_id, participant_type="agent", participant_id=agent).participant_id


def join_and_read_active(storage: Any, session_id: str, agent: str) -> Any:
    """Join `agent`, then read the active agent as the join's caller sees it."""
    join(storage, session_id, agent)
    return storage.sessions.get_active_agent(session_id)


def leave(storage: Any, session_id: str, agent: str) -> bool:
    return storage.sessions.remove_participant(session_id=session_id, participant_type="agent", participant_id=agent)


def select(storage: Any, session_id: str, agent: str) -> Any:
    return storage.sessions.set_active_agent(session_id=session_id, agent_id=agent).active_agent_id


def agents(storage: Any, session_id: str) -> list[str]:
    return [p.participant_id for p in storage.sessions.list_participants(session_id) if p.participant_type == "agent"]


def view(storage: Any, session_id: str) -> dict[str, Any]:
    s = storage.sessions
    session = s.get_session(session_id)
    return {
        "stored": None if session is None else session.active_agent_id,
        "active": s.get_active_agent(session_id),
        "agents": agents(storage, session_id),
    }


def watch_while_held(op: Op, env: Any, waiter_role: str, holder_role: str, seconds: float = HOLD_SECONDS) -> dict[str, Any]:
    """Wait `seconds` while a join is held. On PostgreSQL, poll the server for
    the waiter's backend blocked on a lock the holder's backend holds."""
    deadline = time.monotonic() + seconds
    evidence = None
    while time.monotonic() < deadline and not op.future.done():
        if env.pg and evidence is None:
            rows = env.activity()
            holder = [r["pid"] for r in rows if r["app"] == env.app(holder_role) and r["state"] == "idle in transaction"]
            waiter = [r for r in rows if r["app"] == env.app(waiter_role) and r["wait_event_type"] == "Lock"
                      and set(r["blocked_by"]) & set(holder)]
            if waiter:
                evidence = {"waiter": waiter[0], "holder_pids": holder, "activity": rows}
        time.sleep(0.05)
    return {"finished_while_held": op.future.done(), "seconds_watched": seconds, "pg_lock_wait": evidence}


def pg_wait_ok(env: Any, watch: dict[str, Any]) -> bool:
    """On PostgreSQL the server must show the waiter blocked by the holder; on SQLite nothing to add."""
    return (not env.pg) or watch.get("pg_lock_wait") is not None
