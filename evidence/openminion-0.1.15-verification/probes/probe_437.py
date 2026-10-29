"""#437 on the installed package: APIRuntime on PostgreSQL runtime storage.

    python -I -X utf8 probe_437.py <work_dir> <out.json>

SQLite controls always run (native Windows and Linux). The PostgreSQL cases run
when PGURL (or OPENMINION_TEST_POSTGRES_URL) names a server and the `postgres`
extra is installed. Each PostgreSQL case uses a new, empty database and a URL
with its own `application_name`; the connection counts are server-side
(pg_stat_activity by that application_name).

The garbage collector is disabled before openminion is imported and the probe
never calls gc.collect (calls are counted), so "no connection after close()"
does not depend on a collection.

SQLite controls:
  S1  APIRuntime, no storage backend in the file: storage_connection is the record
      store's own sqlite3.Connection (identity), main database = storage.path; one
      SDK turn; close() closes that connection.
  S2  the same with `storage.backend: sqlite` written in the file.
  S3  OpenMinionRuntime.from_config_path: storage is that same Connection.
  S4  `openminion room create` / `room invite` (console script): rows in state.db.
  S5  the missing-handle error: (a) finalize_runtime_instance with a SQLite record
      store that has no connection, (b) APIRuntime.from_config_path on a SQLite
      config whose built record store reports no connection.
PostgreSQL:
  P1  storage section, postgresql+psycopg:// URL: start, sessions in PostgreSQL,
      one SDK turn (session/message rows), close() -> no connection, no state.db.
  P2  storage section, postgresql:// URL.
  P3  OPENMINION_STORAGE_BACKEND + OPENMINION_STORAGE_POSTGRES_URL over a config
      file without a storage backend.
  P4  OpenMinionRuntime.from_config_path: storage None, sess in PostgreSQL, close().
  P5  the real room CLI (console script): `room create` then `room invite`,
      session/participant rows in PostgreSQL, no connection after exit, no state.db.
"""

from __future__ import annotations

import gc

gc.disable()
_REAL_COLLECT = gc.collect
GC_CALLS: list[str] = []


def _counting_collect(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    import traceback

    GC_CALLS.append(" <- ".join(f"{f.name}" for f in traceback.extract_stack()[-4:-1]))
    return _REAL_COLLECT(*args, **kwargs)


gc.collect = _counting_collect  # type: ignore[assignment]

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import traceback  # noqa: E402
from collections import defaultdict  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import patch  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, quiet, write_result  # noqa: E402
from probe_437_lib import (  # noqa: E402
    RUNTIME_ERROR,
    PgDatabase,
    Reader,
    brief,
    describe_exc,
    install_masking,
    kv,
    mask,
    pg_base,
    pg_versions,
    run_cli,
    sqlite_files,
    storage_path,
    strip_env,
    write_config,
)

PROMPT = "hello from probe_437"
CASES: dict[str, Any] = {}


def scenario(checks: Checks, name: str):
    def wrap(fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            checks.check(f"{name}: completes without an exception", False, observed=mask(describe_exc(exc))[:500])
            traceback.print_exc()
        return fn

    return wrap


def is_closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
        return False
    except sqlite3.ProgrammingError:
        return True


def main_file(conn: sqlite3.Connection) -> str:
    return next(str(r[2]) for r in conn.execute("PRAGMA database_list") if r[1] == "main")


def turn_rows(reader: Reader, session_id: str) -> dict[str, Any]:
    sessions = reader.rows("SELECT id, session_key, channel, target FROM sessions WHERE id = ?", (session_id,))
    messages = reader.rows("SELECT role, body FROM messages WHERE session_id = ? ORDER BY created_at, id", (session_id,))
    participants = reader.rows(
        "SELECT participant_type, participant_id, role FROM room_participants WHERE session_id = ?", (session_id,))
    return {"sessions": sessions, "messages": messages, "room_participants": participants}


def sdk_turn(checks: Checks, label: str, runtime, reader: Reader) -> dict[str, Any]:
    from openminion import Agent

    with Agent(runtime=runtime) as agent:
        result = agent.run(PROMPT)
    rows = turn_rows(reader, str(result.session_id))
    obs = {"run_state": result.run_state, "text": result.text, "session_id": result.session_id, "rows": rows}
    checks.check(f"{label}: one SDK turn (Agent(runtime=...).run) completes", result.run_state == "completed",
                 observed={"run_state": result.run_state, "text": result.text})
    bodies = sorted(m["body"] for m in rows["messages"])
    checks.check(
        f"{label}: the turn's session row and its 2 messages (the prompt and the reply) are in the runtime database",
        len(rows["sessions"]) == 1 and len(rows["messages"]) == 2 and bodies == sorted([PROMPT, result.text]),
        observed={"sessions": len(rows["sessions"]), "messages": [(m["role"], m["body"]) for m in rows["messages"]]},
    )
    parts = [(p["participant_type"], p["participant_id"], p["role"]) for p in rows["room_participants"]]
    checks.check(f"{label}: the turn's session has its owner participant row (agent first, owner)",
                 parts == [("agent", "first", "owner")], observed=parts)
    return obs


# ------------------------------------------------------------------ SQLite controls
def sqlite_controls(checks: Checks, work: Path) -> None:
    from openminion import APIRuntime

    @scenario(checks, "S1 sqlite APIRuntime")
    def _s1():
        root = work / "s1_sqlite_api"
        cfg = write_config(root)
        rt = APIRuntime.from_config_path(str(cfg), home_root=root, data_root=root / ".openminion")
        try:
            conn = rt.storage_connection
            ctx = rt.runtime_storage
            store = ctx.record_store
            checks.check(
                "S1 sqlite: storage_connection is a sqlite3.Connection, the same object as runtime_storage.connection "
                "and the record store's connection",
                isinstance(conn, sqlite3.Connection) and conn is ctx.connection and conn is store.connection,
                observed={"type": type(conn).__name__, "record_store": type(store).__name__,
                          "is_runtime_storage_connection": conn is ctx.connection,
                          "is_record_store_connection": conn is getattr(store, "connection", None)},
            )
            path = storage_path(root)
            checks.check("S1 sqlite: that connection's main database is storage.path, and the file exists",
                         os.path.normcase(main_file(conn)) == os.path.normcase(str(path.resolve())) and path.is_file(),
                         observed={"main": main_file(conn), "storage.path": str(path)})
            CASES["S1"] = sdk_turn(checks, "S1 sqlite", rt, Reader(sqlite_path=path))
        finally:
            rt.close()
        checks.check("S1 sqlite: close() closes that connection", is_closed(conn), observed=is_closed(conn))

    @scenario(checks, "S2 sqlite explicit backend")
    def _s2():
        root = work / "s2_sqlite_explicit"
        cfg = write_config(root, storage={"backend": "sqlite"})
        rt = APIRuntime.from_config_path(str(cfg), home_root=root, data_root=root / ".openminion")
        try:
            conn = rt.storage_connection
            checks.check(
                "S2 sqlite (`storage.backend: sqlite` in the file): record backend record.sqlite; storage_connection "
                "is the record store's own sqlite3.Connection",
                rt.config.storage.record_backend() == "record.sqlite" and isinstance(conn, sqlite3.Connection)
                and conn is rt.runtime_storage.connection and conn is rt.runtime_storage.record_store.connection,
                observed={"record_backend": rt.config.storage.record_backend(), "type": type(conn).__name__},
            )
        finally:
            rt.close()
        checks.check("S2 sqlite: close() closes it", is_closed(conn), observed=is_closed(conn))

    @scenario(checks, "S3 sqlite wrapper")
    def _s3():
        from openminion.services.runtime import OpenMinionRuntime

        root = work / "s3_sqlite_wrapper"
        cfg = write_config(root)
        w = OpenMinionRuntime.from_config_path(str(cfg), home_root=str(root), data_root=str(root / ".openminion"))
        try:
            rm = w.runtime_manager
            storage = w.storage
            checks.check(
                "S3 sqlite wrapper: OpenMinionRuntime.storage is the runtime's storage_connection, the record store's "
                "own sqlite3.Connection; sess is the runtime's SessionStore",
                isinstance(storage, sqlite3.Connection) and storage is rm.storage_connection
                and storage is rm.runtime_storage.connection and w.sess is rm.sessions,
                observed={"type": type(storage).__name__, "sess": type(w.sess).__name__},
            )
        finally:
            w.close()
        checks.check("S3 sqlite wrapper: close() closes it", is_closed(storage), observed=is_closed(storage))

    @scenario(checks, "S4 sqlite room CLI")
    def _s4():
        root = work / "s4_sqlite_room"
        cfg = write_config(root, agents=("first", "second"))
        created = run_cli(root, cfg, ["room", "create", "--name", "probe-437", "--agent", "first", "--agent", "second"])
        out = kv(created["stdout"])
        room = out.get("room", "")
        invited = run_cli(root, cfg, ["room", "invite", room, "--human", "alice"])
        reader = Reader(sqlite_path=storage_path(root))
        rows = reader.rows("SELECT participant_type, participant_id, role, join_seq FROM room_participants "
                           "WHERE session_id = ? ORDER BY join_seq", (room,))
        CASES["S4"] = {"create": brief(created), "invite": brief(invited), "participants": rows}
        checks.check(
            "S4 sqlite: `openminion room create --agent first --agent second` and `room invite <room> --human alice` "
            "exit 0 (participants=2, active_agent=first, then participants=3); the rows are in state.db",
            created["exit_code"] == 0 and out.get("participants") == "2" and out.get("active_agent") == "first"
            and invited["exit_code"] == 0 and "participants=3" in invited["stdout"]
            and [(r["participant_type"], r["participant_id"], r["role"], r["join_seq"]) for r in rows]
            == [("agent", "first", "owner", 1), ("agent", "second", "participant", 2), ("human", "alice", "participant", 3)],
            observed={"create": brief(created), "invite": brief(invited), "rows": rows},
        )

    @scenario(checks, "S5a missing handle, finalize_runtime_instance")
    def _s5a():
        from openminion.api.core.bootstrap import finalize_runtime_instance
        from openminion.base.config import OpenMinionConfig
        from openminion.modules.storage.runtime.context import RuntimeStorageContext

        root = work / "s5a"
        ctx = RuntimeStorageContext(sqlite_path=root / "state.db", migration_result=object(),
                                    record_store=SimpleNamespace(), sessions=object(), idempotency=object())
        built: list[int] = []

        class UnexpectedRuntime:
            def __init__(self, **fields: object) -> None:
                built.append(1)

        paths = SimpleNamespace(config=root / "agents.json", home=root, data=root / ".openminion",
                                storage=root / "state.db", memory=root / "memory")
        try:
            finalize_runtime_instance(
                cls=UnexpectedRuntime, base_config=OpenMinionConfig(), manager=object(), paths=paths,
                infrastructure=defaultdict(object, runtime_storage=ctx), agent=object(), gateway=object(),
                runtime_mode="direct", fallback_reason="", effective_run_profile_overrides=object(),
            )
            outcome = "returned"
        except Exception as exc:  # noqa: BLE001
            outcome = describe_exc(exc)
        CASES["S5a"] = outcome
        checks.check(
            "S5a sqlite (default storage config): finalize_runtime_instance with a record store that has no "
            f"connection raises the existing `RuntimeError: {RUNTIME_ERROR}` and builds no runtime",
            outcome == f"RuntimeError: {RUNTIME_ERROR}" and not built,
            observed={"outcome": outcome, "runtime_built": bool(built)},
        )

    @scenario(checks, "S5b missing handle, APIRuntime.from_config_path")
    def _s5b():
        import openminion.api.core.infrastructure as infra

        real = infra.build_runtime_storage
        built: list[tuple[Any, Any]] = []

        class NoHandle:
            """The real SQLite record store, except that it reports no connection."""

            connection = None

            def __init__(self, inner: Any) -> None:
                object.__setattr__(self, "_inner", inner)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

        def hiding(*args: Any, **kwargs: Any):
            ctx = real(*args, **kwargs)
            built.append((ctx, ctx.record_store))
            object.__setattr__(ctx, "record_store", NoHandle(ctx.record_store))
            return ctx

        root = work / "s5b_sqlite_no_handle"
        cfg = write_config(root, storage={"backend": "sqlite"})
        try:
            with patch.object(infra, "build_runtime_storage", hiding):
                rt = APIRuntime.from_config_path(str(cfg), home_root=root, data_root=root / ".openminion")
            rt.close()
            outcome = "started"
        except Exception as exc:  # noqa: BLE001
            outcome = describe_exc(exc)
        finally:
            for ctx, store in built:
                object.__setattr__(ctx, "record_store", store)
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass
        CASES["S5b"] = outcome
        checks.check(
            "S5b sqlite (`storage.backend: sqlite`): APIRuntime.from_config_path whose built SQLite record store "
            f"reports no connection fails with the existing `RuntimeError: {RUNTIME_ERROR}`",
            outcome == f"RuntimeError: {RUNTIME_ERROR}" and len(built) == 1,
            observed={"outcome": outcome, "storages_built": len(built)},
        )


# ------------------------------------------------------------------ PostgreSQL
def pg_start_case(checks: Checks, work: Path, form: str, *, turn: bool) -> None:
    from openminion import APIRuntime

    label = {"config": "P1 postgres, storage section, postgresql+psycopg:// URL",
             "plain": "P2 postgres, storage section, postgresql:// URL",
             "env": "P3 postgres, OPENMINION_STORAGE_BACKEND/OPENMINION_STORAGE_POSTGRES_URL"}[form]
    tag = label.split()[0]
    db = PgDatabase(f"om437_{form}")
    root = work / f"pg_{form}"
    env: dict[str, str] = {}
    if form == "config":
        cfg = write_config(root, storage={"backend": "postgres", "postgres_url": db.url(scheme="postgresql+psycopg")})
    elif form == "plain":
        cfg = write_config(root, storage={"backend": "postgres", "postgres_url": db.url(scheme="postgresql")})
    else:
        cfg = write_config(root)  # storage.path only: the file selects no backend
        env = {"OPENMINION_STORAGE_BACKEND": "postgres", "OPENMINION_STORAGE_POSTGRES_URL": db.url(scheme="postgresql+psycopg")}
    obs: dict[str, Any] = {"database": db.name, "application_name": db.app,
                           "config_storage": json.loads(cfg.read_text(encoding="utf-8"))["storage"],
                           "env": sorted(env)}
    CASES[tag] = obs
    os.environ.update(env)
    try:
        try:
            rt = APIRuntime.from_config_path(str(cfg), home_root=root, data_root=root / ".openminion")
        except Exception as exc:  # noqa: BLE001
            obs["start"] = mask(describe_exc(exc))
            obs["traceback_tail"] = mask(traceback.format_exc()).strip().splitlines()[-6:]
            checks.check(f"{label}: APIRuntime.from_config_path starts", False, observed=obs["start"])
            obs["connections_after_failed_start_no_gc"] = db.connections_settled(timeout=2)
            obs["storage_path_exists"] = storage_path(root).exists()
            return
        obs["start"] = "started"
        try:
            store = rt.runtime_storage.record_store
            checks.check(
                f"{label}: starts; storage.backend postgres, record store RecordStorePostgres, storage_connection is None",
                rt.config.storage.backend == "postgres" and rt.config.storage.record_backend() == "record.postgres"
                and type(store).__name__ == "RecordStorePostgres" and rt.storage_connection is None,
                observed={"backend": rt.config.storage.backend, "record_backend": rt.config.storage.record_backend(),
                          "record_store": type(store).__name__, "storage_connection": repr(rt.storage_connection)},
            )
            try:
                rt.runtime_storage.connection
                obs["runtime_storage.connection"] = "returned"
            except Exception as exc:  # noqa: BLE001
                obs["runtime_storage.connection"] = describe_exc(exc)
            session = rt.sessions.resolve_session(agent_id="first", channel="console", target=f"om437-{form}")
            rt.sessions.append_message(session_id=session.id, role="inbound", body=f"written through runtime.sessions ({form})")
            read = db.rows("SELECT s.target, m.body FROM sessions s JOIN messages m ON m.session_id = s.id WHERE s.id = :id",
                           id=session.id)
            checks.check(
                f"{label}: sessions use PostgreSQL: a session and a message written through runtime.sessions are read "
                "back from an independent PostgreSQL connection",
                read == [{"target": f"om437-{form}", "body": f"written through runtime.sessions ({form})"}],
                observed=read,
            )
            ledger = db.rows("SELECT count(*) AS n, max(version) AS head FROM migrations")
            obs["postgres_runtime_ledger"] = ledger
            if turn:
                obs["turn"] = sdk_turn(checks, label, rt, Reader(db=db))
            open_count = db.connections_now()
            obs["connections_while_open"] = open_count
            checks.check(f"{label}: while open, the runtime holds PostgreSQL connections under its application_name",
                         open_count > 0, observed=open_count)
        finally:
            rt.close()
        after = db.connections_settled()
        obs["connections_after_close"] = after
        checks.check(f"{label}: after close() no connection with its application_name remains (no gc.collect)",
                     after["count"] == 0, observed=after)
        files = sqlite_files(root)
        obs["sqlite_files_under_root"] = files
        checks.check(f"{label}: no runtime SQLite database created at storage.path",
                     not storage_path(root).exists() and ".openminion/state.db" not in files,
                     observed={"storage.path exists": storage_path(root).exists(), "sqlite files": sorted(files)})
        obs["postgres_totals"] = db.rows("SELECT (SELECT count(*) FROM sessions) AS sessions, "
                                         "(SELECT count(*) FROM messages) AS messages, "
                                         "(SELECT count(*) FROM room_participants) AS room_participants")
    finally:
        for key in env:
            os.environ.pop(key, None)


def pg_wrapper_case(checks: Checks, work: Path) -> None:
    from openminion.services.runtime import OpenMinionRuntime

    label = "P4 postgres wrapper"
    db = PgDatabase("om437_wrapper")
    root = work / "pg_wrapper"
    cfg = write_config(root, storage={"backend": "postgres", "postgres_url": db.url(scheme="postgresql+psycopg")})
    obs: dict[str, Any] = {"database": db.name, "application_name": db.app}
    CASES["P4"] = obs
    try:
        w = OpenMinionRuntime.from_config_path(str(cfg), home_root=str(root), data_root=str(root / ".openminion"))
    except Exception as exc:  # noqa: BLE001
        obs["start"] = mask(describe_exc(exc))
        checks.check(f"{label}: OpenMinionRuntime.from_config_path starts", False, observed=obs["start"])
        obs["connections_after_failed_start_no_gc"] = db.connections_settled(timeout=2)
        return
    obs["start"] = "started"
    try:
        rm = w.runtime_manager
        checks.check(
            f"{label}: OpenMinionRuntime.storage is None; sess is the runtime's SessionStore over RecordStorePostgres; "
            "the runtime's storage_connection is None",
            w.storage is None and w.sess is rm.sessions and type(w.sess).__name__ == "SessionStore"
            and type(rm.runtime_storage.record_store).__name__ == "RecordStorePostgres" and rm.storage_connection is None,
            observed={"storage": repr(w.storage), "sess": type(w.sess).__name__,
                      "record_store": type(rm.runtime_storage.record_store).__name__},
        )
        s = w.sess.resolve_session(agent_id="first", channel="console", target="om437-wrapper")
        read = db.rows("SELECT target FROM sessions WHERE id = :id", id=s.id)
        checks.check(f"{label}: a session written through wrapper.sess is in PostgreSQL (independent read)",
                     read == [{"target": "om437-wrapper"}], observed=read)
        open_count = db.connections_now()
        obs["connections_while_open"] = open_count
        checks.check(f"{label}: while open, PostgreSQL connections under its application_name", open_count > 0,
                     observed=open_count)
    finally:
        w.close()
    after = db.connections_settled()
    obs["connections_after_close"] = after
    checks.check(f"{label}: after wrapper.close() no connection with its application_name remains (no gc.collect)",
                 after["count"] == 0, observed=after)
    checks.check(f"{label}: no runtime SQLite database created at storage.path", not storage_path(root).exists(),
                 observed=sorted(sqlite_files(root)))


def pg_room_cli_case(checks: Checks, work: Path) -> None:
    label = "P5 postgres room CLI"
    db = PgDatabase("om437_room")
    root = work / "pg_room"
    cfg = write_config(root, agents=("first", "second"),
                       storage={"backend": "postgres", "postgres_url": db.url(scheme="postgresql+psycopg")})
    created = run_cli(root, cfg, ["room", "create", "--name", "probe-437", "--agent", "first", "--agent", "second"], watch=db)
    out = kv(created["stdout"])
    room = out.get("room", "")
    obs: dict[str, Any] = {"database": db.name, "application_name": db.app, "create": brief(created)}
    CASES["P5"] = obs
    checks.check(
        f"{label}: `openminion ... room create --name probe-437 --agent first --agent second` exits 0 with "
        "participants=2, active_agent=first",
        created["exit_code"] == 0 and out.get("participants") == "2" and out.get("active_agent") == "first",
        observed=brief(created),
    )
    if created["exit_code"] != 0:
        obs["storage_path_exists"] = storage_path(root).exists()
        return
    invited = run_cli(root, cfg, ["room", "invite", room, "--human", "alice"], watch=db)
    obs["invite"] = brief(invited)
    checks.check(f"{label}: `openminion ... room invite <room> --human alice` exits 0 with participants=3",
                 invited["exit_code"] == 0 and "participants=3" in invited["stdout"], observed=brief(invited))
    session = db.rows("SELECT id, session_key, channel, target, metadata_json, active_agent_id FROM sessions WHERE id = :id",
                      id=room)
    parts = db.rows("SELECT participant_type, participant_id, role, join_seq, left_at FROM room_participants "
                    "WHERE session_id = :id ORDER BY join_seq", id=room)
    obs["postgres_session"] = session
    obs["postgres_participants"] = parts
    meta = json.loads(session[0]["metadata_json"]) if session else {}
    checks.check(
        f"{label}: the room's session row is in PostgreSQL (room session key, name probe-437, active agent first)",
        len(session) == 1 and str(session[0]["session_key"]).startswith("room:") and meta.get("name") == "probe-437"
        and session[0]["active_agent_id"] == "first",
        observed={"session_key": session[0]["session_key"] if session else None, "metadata": meta,
                  "active_agent_id": session[0]["active_agent_id"] if session else None},
    )
    checks.check(
        f"{label}: its participant rows are in PostgreSQL: agent first (owner, join_seq 1), agent second "
        "(participant, 2), human alice (participant, 3)",
        [(p["participant_type"], p["participant_id"], p["role"], p["join_seq"], p["left_at"]) for p in parts]
        == [("agent", "first", "owner", 1, None), ("agent", "second", "participant", 2, None),
            ("human", "alice", "participant", 3, None)],
        observed=parts,
    )
    checks.check(
        f"{label}: each CLI process connected to PostgreSQL under its application_name while running, and none "
        "remains after it exits",
        created["connections_max_while_running"] > 0 and invited["connections_max_while_running"] > 0
        and created["connections_after_exit"]["count"] == 0 and invited["connections_after_exit"]["count"] == 0,
        observed={"create": [created["connections_max_while_running"], created["connections_after_exit"]],
                  "invite": [invited["connections_max_while_running"], invited["connections_after_exit"]]},
    )
    files = sqlite_files(root)
    obs["sqlite_files_under_root"] = files
    checks.check(f"{label}: no runtime SQLite database created at storage.path",
                 not storage_path(root).exists() and ".openminion/state.db" not in files,
                 observed={"storage.path exists": storage_path(root).exists(), "sqlite files": sorted(files)})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("out")
    args = ap.parse_args()
    install_masking()
    strip_env()
    quiet()
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    pg = bool(pg_base())
    extra: dict[str, Any] = {"gc": "disabled before importing openminion; gc.collect calls counted"}
    extra["postgres"] = pg_versions() if pg else "not run here (no PGURL): SQLite controls only"
    info = banner("probe_437", **extra)
    checks = Checks()
    sqlite_controls(checks, work)
    if pg:
        for form in ("config", "plain", "env"):
            @scenario(checks, f"postgres start ({form})")
            def _start(form: str = form) -> None:
                pg_start_case(checks, work, form, turn=(form == "config"))

        @scenario(checks, "postgres wrapper")
        def _wrapper() -> None:
            pg_wrapper_case(checks, work)

        @scenario(checks, "postgres room CLI")
        def _room() -> None:
            pg_room_cli_case(checks, work)

    summary = checks.summary()
    payload = {"banner": info, "checks": checks.items, "cases": CASES, "gc_collect_calls": GC_CALLS,
               "summary": summary}
    write_result(Path(args.out), json.loads(mask(json.dumps(payload, default=str))))
    print(f"gc.collect calls during the probe: {len(GC_CALLS)}")
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
