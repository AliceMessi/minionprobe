"""Where a started runtime keeps its data, on SQLite and on PostgreSQL runtime storage.

    python -I probe_runtime_postgres_data.py --label <name> --work <dir> --out <out.json>

Needs PGURL (a PostgreSQL server URL) and the `postgres` extra (installed package
only). Each PostgreSQL case uses a new, empty database on that server and a
minimal hand-written config file with one echo agent. Cases, each on SQLite
(storage section absent) and on PostgreSQL (`storage.backend: postgres`):

  api      APIRuntime.from_config_path, then one turn through Agent(runtime=...),
           then close()
  wrapper  OpenMinionRuntime.from_config_path, then close()
  room     the installed console script: `openminion ... room create`

For the PostgreSQL cases it records the server connections to that database
while the runtime is open and after close() (after gc.collect()), the session,
message, room and participant rows in PostgreSQL, and, for every case, the
SQLite files under the runtime home with their `sessions` row count.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import sqlalchemy as sa

PGURL = os.environ.get("PGURL", "")
STRIP_ENV = (
    "OPENMINION_HOME",
    "OPENMINION_DATA_ROOT",
    "OPENMINION_GENERATED_ROOT",
    "OPENMINION_CONFIG_PATH",
    "OPENMINION_STORAGE_BACKEND",
    "OPENMINION_STORAGE_POSTGRES_URL",
)


def _prefixes() -> list[tuple[str, str]]:
    pairs = []
    for value, token in (
        (sys.prefix, "<VENV>"),
        (os.environ.get("RUNNER_TEMP"), "<RUNNER_TEMP>"),
        (str(Path.home()), "<USER_HOME>"),
    ):
        if value:
            pairs.append((str(Path(value)), token))
            pairs.append((str(Path(value).resolve()), token))
    return sorted(set(pairs), key=lambda item: len(item[0]), reverse=True)


PREFIXES = _prefixes()


def clean(text: str) -> str:
    for raw, token in PREFIXES:
        text = text.replace(raw, token)
    # The runner's PostgreSQL password is public, but keep it out of the logs.
    return re.sub(r"(postgresql(?:\+\w+)?://[^:/@\s]+):[^@\s]*@", r"\1:***@", text)


def clean_any(value: Any) -> Any:
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, dict):
        return {k: clean_any(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_any(v) for v in value]
    return value


def db_url(name: str) -> str:
    return urlunsplit(urlsplit(PGURL)._replace(path=f"/{name}"))


def create_database(name: str) -> str:
    engine = sa.create_engine(db_url("postgres"), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    return db_url(name)


def query(name: str, sql: str, **params: Any) -> list[list[Any]]:
    engine = sa.create_engine(db_url(name))
    try:
        with engine.connect() as conn:
            return [list(row) for row in conn.execute(sa.text(sql), params)]
    finally:
        engine.dispose()


def connections(name: str) -> int:
    """Other server connections to database `name`, after gc.collect()."""
    gc.collect()
    return query(
        name,
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = :db AND pid <> pg_backend_pid()",
        db=name,
    )[0][0]


def write_config(root: Path, storage: dict[str, Any]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    config = {
        "default_agent": "first",
        "agents": {"first": {"provider": "echo", "default_channel": "console"}},
        "runtime": {"process_mode": "single-process", "daemon_auto_start": False},
        "storage": {"path": str(root / ".openminion" / "state.db"), **storage},
    }
    path = root / "agents.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def sqlite_files(root: Path) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.read_bytes()[:16] != b"SQLite format 3\x00":
            continue
        conn = sqlite3.connect(f"file:{path.as_posix()}?immutable=1", uri=True)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            rows = conn.execute("SELECT count(*) FROM sessions").fetchone()[0] if "sessions" in tables else None
        finally:
            conn.close()
        found[path.relative_to(root).as_posix()] = {"tables": len(tables), "sessions_rows": rows}
    return found


def postgres_rows(name: str) -> dict[str, Any]:
    return {
        "sessions": query(name, "SELECT count(*) FROM sessions")[0][0],
        "messages": query(name, "SELECT count(*) FROM messages")[0][0],
        "room_participants": query(
            name, "SELECT session_id, participant_type, participant_id, role FROM room_participants"
        ),
    }


def failure(exc: BaseException) -> dict[str, Any]:
    return {"outcome": "failed", "exception": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}


def case_api(root: Path, storage: dict[str, Any], name: str | None) -> dict[str, Any]:
    from openminion import Agent, APIRuntime

    config = write_config(root, storage)
    try:
        runtime = APIRuntime.from_config_path(str(config), home_root=root, data_root=root / ".openminion")
    except Exception as exc:  # noqa: BLE001
        result = failure(exc)
    else:
        result = {"outcome": "started"}
        try:
            result["record_store"] = type(runtime.runtime_storage.record_store).__name__
            result["storage_connection"] = type(runtime.storage_connection).__name__
            with Agent(runtime=runtime) as agent:
                turn = agent.run("hello")
            result["turn"] = {"run_state": turn.run_state, "text": turn.text}
            if name:
                result["connections_open"] = connections(name)
        except Exception as exc:  # noqa: BLE001
            result["turn_error"] = failure(exc)
        finally:
            runtime.close()
    if name:
        result["connections_after_close"] = connections(name)
        if result["outcome"] == "started":
            result["postgres_rows"] = postgres_rows(name)
    result["sqlite_files"] = sqlite_files(root)
    return result


def case_wrapper(root: Path, storage: dict[str, Any], name: str | None) -> dict[str, Any]:
    from openminion.services.runtime import OpenMinionRuntime

    config = write_config(root, storage)
    try:
        wrapper = OpenMinionRuntime.from_config_path(
            str(config), home_root=str(root), data_root=str(root / ".openminion")
        )
    except Exception as exc:  # noqa: BLE001
        result = failure(exc)
    else:
        try:
            result = {
                "outcome": "started",
                "storage": type(wrapper.storage).__name__,
                "storage_is_runtime_storage_connection": wrapper.storage is wrapper.runtime_manager.storage_connection,
                "sess": type(wrapper.sess).__name__,
                "record_store": type(wrapper.runtime_manager.runtime_storage.record_store).__name__,
            }
        finally:
            wrapper.close()
    if name:
        result["connections_after_close"] = connections(name)
    return result


def case_room(root: Path, storage: dict[str, Any], name: str | None, binary: str) -> dict[str, Any]:
    config = write_config(root, storage)
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    done = subprocess.run(
        [binary, "--home-root", str(root), "--data-root", str(root / ".openminion"), "--config", str(config),
         "room", "create", "--name", "probe-room", "--agent", "first"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=300, check=False,
    )
    stderr = done.stderr.strip().splitlines()
    result: dict[str, Any] = {
        "exit_code": done.returncode,
        "stdout": done.stdout,
        "stderr_last_line": stderr[-1] if stderr else "",
    }
    if name:
        result["connections_after"] = connections(name)
        result["postgres_rows"] = postgres_rows(name) if done.returncode == 0 else None
    result["sqlite_files"] = sqlite_files(root)
    return result


def versions() -> dict[str, Any]:
    import openminion
    import psycopg

    info = {
        "openminion": getattr(openminion, "__version__", "?"),
        "openminion_file": openminion.__file__,
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "sqlalchemy": sa.__version__,
        "psycopg": psycopg.__version__,
        "platform": platform.platform(),
    }
    info["postgres_server"] = query("postgres", "SHOW server_version")[0][0]
    return info


def summary(name: str, result: dict[str, Any]) -> str:
    keep = {k: v for k, v in result.items() if k not in ("traceback", "sqlite_files", "stdout")}
    if isinstance(keep.get("turn_error"), dict):
        keep["turn_error"] = keep["turn_error"]["exception"]
    if "stdout" in result:
        keep["stdout"] = " ".join(result["stdout"].split())
    files = result.get("sqlite_files", {})
    keep["sqlite_files_with_sessions"] = {k: v["sessions_rows"] for k, v in files.items() if v["sessions_rows"] is not None}
    return f"CASE {name}: {json.dumps(keep, sort_keys=True, default=str)}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--openminion-bin", default=str(Path(sys.executable).with_name("openminion")))
    args = ap.parse_args()
    if not PGURL:
        print("PGURL is not set", file=sys.stderr)
        return 2
    for key in STRIP_ENV:
        os.environ.pop(key, None)
    label = re.sub(r"[^a-z0-9]", "_", args.label.lower())
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"label": args.label, "versions": versions(), "cases": {}}
    print(clean(f"PROBE runtime_postgres_data label={args.label} {json.dumps(report['versions'], sort_keys=True)}"), flush=True)
    for kind in ("api", "wrapper", "room"):
        for backend in ("sqlite", "postgres"):
            case = f"{kind}_{backend}"
            name = None
            storage: dict[str, Any] = {}
            if backend == "postgres":
                name = f"om437_{label}_{kind}_{uuid.uuid4().hex[:6]}"
                storage = {"backend": "postgres", "postgres_url": create_database(name)}
            try:
                if kind == "api":
                    result = case_api(work / case, storage, name)
                elif kind == "wrapper":
                    result = case_wrapper(work / case, storage, name)
                else:
                    result = case_room(work / case, storage, name, args.openminion_bin)
            except Exception as exc:  # noqa: BLE001
                result = {"probe_error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
            report["cases"][case] = result
            print(clean(summary(case, result)), flush=True)
            if result.get("outcome") == "failed":
                print(clean(f"TRACEBACK {case}:\n{result['traceback']}"), flush=True)
    Path(args.out).write_text(
        json.dumps(clean_any(report), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
