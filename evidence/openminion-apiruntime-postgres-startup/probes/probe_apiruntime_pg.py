"""APIRuntime startup on a PostgreSQL runtime database (installed package only).

    python -I probe_apiruntime_pg.py --label <name> --work <dir> --out <out.json>

Needs PGURL (a PostgreSQL server URL, e.g. postgresql://postgres:pw@localhost:5432/postgres)
and the `postgres` extra. Each case uses a new, empty database on that server
and a minimal hand-written config file. Cases:

  sqlite_api      APIRuntime.from_config_path, storage.backend absent (SQLite)  control
  postgres_api    the same config plus storage.backend/postgres_url
  postgres_env    the SQLite config file, backend chosen by OPENMINION_STORAGE_BACKEND
                  and OPENMINION_STORAGE_POSTGRES_URL (the route StorageConfigError names)
  storage_direct  build_runtime_storage with the same record backend/options, then
                  RuntimeStorageContext.connection
  cli_room_*      the installed console script: `openminion ... room create`
  cli_sessions_*  the installed console script: `openminion ... sessions list --json`
  postgres_api_after_storage_migrate
                  `openminion storage migrate --backend postgres` on a new database
                  first (every module), then APIRuntime.from_config_path
  postgres_api_plain_url
                  postgres_api with a `postgresql://` URL (no driver name)

For the PostgreSQL cases it records the database facts after the attempt: the
`migrations` ledger, the number of tables, and how many other connections to
that database are still open after gc.collect().
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
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import openminion

PGURL = os.environ.get("PGURL", "")
STRIP_ENV = (
    "OPENMINION_HOME",
    "OPENMINION_DATA_ROOT",
    "OPENMINION_GENERATED_ROOT",
    "OPENMINION_CONFIG_PATH",
    "OPENMINION_STORAGE_BACKEND",
    "OPENMINION_STORAGE_POSTGRES_URL",
)


# ---------------------------------------------------------------- sanitizing
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


def log(line: str) -> None:
    print(clean(line), flush=True)


# ---------------------------------------------------------------- PostgreSQL
def db_url(name: str) -> str:
    parts = urlsplit(PGURL)
    return urlunsplit(parts._replace(path=f"/{name}"))


def admin_engine():
    import sqlalchemy as sa

    return sa.create_engine(db_url("postgres"), isolation_level="AUTOCOMMIT", future=True)


def create_database(name: str) -> str:
    import sqlalchemy as sa

    engine = admin_engine()
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    return db_url(name)


def database_facts(name: str) -> dict[str, Any]:
    """Tables, the migrations ledger and other open connections to `name`."""
    import sqlalchemy as sa

    gc.collect()
    facts: dict[str, Any] = {}
    engine = sa.create_engine(db_url(name), future=True)
    try:
        with engine.connect() as conn:
            facts["public_tables"] = conn.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            ).scalar_one()
            has_ledger = conn.execute(
                sa.text("SELECT to_regclass('public.migrations') IS NOT NULL")
            ).scalar_one()
            if has_ledger:
                row = conn.execute(
                    sa.text("SELECT count(*), max(version) FROM migrations")
                ).one()
                facts["migrations_rows"] = int(row[0])
                facts["migrations_max_version"] = row[1]
            else:
                facts["migrations_rows"] = 0
                facts["migrations_max_version"] = None
            facts["other_open_connections"] = conn.execute(
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {"db": name},
            ).scalar_one()
    finally:
        engine.dispose()
    return facts


# ---------------------------------------------------------------- configs
def write_config(root: Path, storage: dict[str, Any]) -> Path:
    """The minimal config file: one offline `echo` agent and the storage section."""
    config = {
        "default_agent": "first",
        "agents": {"first": {"provider": "echo", "default_channel": "console"}},
        "runtime": {"process_mode": "single-process", "daemon_auto_start": False},
        "storage": {"path": str(root / ".openminion" / "state.db"), **storage},
    }
    root.mkdir(parents=True, exist_ok=True)
    path = root / "agents.json"
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def roots(root: Path) -> dict[str, Any]:
    return {"home_root": root, "data_root": root / ".openminion"}


def failure(exc: BaseException) -> dict[str, Any]:
    return {
        "outcome": "failed",
        "exception": f"{type(exc).__name__}: {exc}",
        "traceback": traceback.format_exc(),
    }


def try_api_runtime(config_path: Path, root: Path) -> dict[str, Any]:
    from openminion import APIRuntime

    try:
        runtime = APIRuntime.from_config_path(str(config_path), **roots(root))
    except Exception as exc:  # noqa: BLE001
        return failure(exc)
    try:
        return {
            "outcome": "started",
            "record_store": type(runtime.runtime_storage.record_store).__name__,
            "storage_connection": type(runtime.storage_connection).__name__,
        }
    finally:
        runtime.close()


# ---------------------------------------------------------------- cases
def case_sqlite_api(work: Path) -> dict[str, Any]:
    root = work / "sqlite_api"
    cfg = write_config(root, {})
    result = try_api_runtime(cfg, root)
    result["config"] = json.loads(cfg.read_text(encoding="utf-8"))
    return result


def case_postgres_api(work: Path, label: str) -> dict[str, Any]:
    name = f"om_{label}_api"
    url = create_database(name)
    before = database_facts(name)
    root = work / "postgres_api"
    cfg = write_config(root, {"backend": "postgres", "postgres_url": url})
    result = try_api_runtime(cfg, root)
    result["config"] = json.loads(cfg.read_text(encoding="utf-8"))
    result["database_before"] = before
    result["database_after"] = database_facts(name)
    return result


def case_postgres_env(work: Path, label: str) -> dict[str, Any]:
    name = f"om_{label}_env"
    url = create_database(name)
    root = work / "postgres_env"
    cfg = write_config(root, {})
    os.environ["OPENMINION_STORAGE_BACKEND"] = "postgres"
    os.environ["OPENMINION_STORAGE_POSTGRES_URL"] = url
    try:
        result = try_api_runtime(cfg, root)
    finally:
        os.environ.pop("OPENMINION_STORAGE_BACKEND", None)
        os.environ.pop("OPENMINION_STORAGE_POSTGRES_URL", None)
    result["env"] = {
        "OPENMINION_STORAGE_BACKEND": "postgres",
        "OPENMINION_STORAGE_POSTGRES_URL": url,
    }
    result["database_after"] = database_facts(name)
    return result


def case_storage_direct(work: Path, label: str) -> dict[str, Any]:
    """The storage the APIRuntime infrastructure builds, then the bootstrap's property read."""
    from openminion.base.config.parser import openminion_config_from_dict
    from openminion.modules.storage.runtime.context import build_runtime_storage

    name = f"om_{label}_direct"
    url = create_database(name)
    root = work / "storage_direct"
    cfg = write_config(root, {"backend": "postgres", "postgres_url": url})
    config = openminion_config_from_dict(json.loads(cfg.read_text(encoding="utf-8")))
    result: dict[str, Any] = {
        "record_backend": config.storage.record_backend(),
    }
    storage = build_runtime_storage(
        config.storage.path,
        env={},
        record_backend=config.storage.record_backend(),
        record_backend_options=config.storage.record_backend_options(),
    )
    try:
        result["record_store"] = type(storage.record_store).__name__
        result["migration_current_version"] = storage.migration_result.current_version
        result["sessions"] = type(storage.sessions).__name__
        try:
            result["connection"] = type(storage.connection).__name__
        except Exception as exc:  # noqa: BLE001
            result["connection"] = f"{type(exc).__name__}: {exc}"
    finally:
        storage.close()
    return result


def run_cli(binary: str, root: Path, cfg: Path, argv: list[str]) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    cmd = [
        binary,
        "--home-root", str(root),
        "--data-root", str(root / ".openminion"),
        "--config", str(cfg),
        *argv,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(root), timeout=300,
    )
    return {
        "argv": ["openminion", *cmd[1:]],
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


def case_cli(work: Path, label: str, binary: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    room = ["room", "create", "--name", "probe-room", "--agent", "first"]
    sessions = ["sessions", "list", "--json"]
    sqlite_root = work / "cli_sqlite"
    sqlite_cfg = write_config(sqlite_root, {})
    out["cli_room_sqlite"] = run_cli(binary, sqlite_root, sqlite_cfg, room)
    out["cli_sessions_sqlite"] = run_cli(binary, sqlite_root, sqlite_cfg, sessions)
    name = f"om_{label}_cli"
    url = create_database(name)
    pg_root = work / "cli_postgres"
    pg_cfg = write_config(pg_root, {"backend": "postgres", "postgres_url": url})
    out["cli_room_postgres"] = run_cli(binary, pg_root, pg_cfg, room)
    out["cli_room_postgres"]["database_after"] = database_facts(name)
    out["cli_sessions_postgres"] = run_cli(binary, pg_root, pg_cfg, sessions)
    return out


def case_migrate_then_api(work: Path, label: str, binary: str) -> dict[str, Any]:
    """`openminion storage migrate` (every module) first, as the startup warnings ask."""
    name = f"om_{label}_migrated"
    url = create_database(name)
    root = work / "migrated"
    cfg = write_config(root, {"backend": "postgres", "postgres_url": url})
    migrate = run_cli(
        binary, root, cfg,
        ["storage", "migrate", "--backend", "postgres", "--postgres-url", url, "--json"],
    )
    try:
        modules = json.loads(migrate["stdout"]).get("modules", [])
        migrate["module_status"] = {m.get("module_id"): m.get("status") for m in modules}
    except (ValueError, AttributeError):
        migrate["module_status"] = None
    migrate["stdout"] = migrate["stdout"][-600:]
    result = try_api_runtime(cfg, root)
    result["storage_migrate"] = migrate
    result["database_after"] = database_facts(name)
    return result


def case_plain_url(work: Path, label: str) -> dict[str, Any]:
    """The same config with a `postgresql://` URL (no driver name)."""
    name = f"om_{label}_plain"
    url = create_database(name)
    plain = re.sub(r"^postgresql\+\w+://", "postgresql://", url)
    root = work / "plain_url"
    cfg = write_config(root, {"backend": "postgres", "postgres_url": plain})
    result = try_api_runtime(cfg, root)
    result["postgres_url_scheme"] = plain.split("://", 1)[0]
    result["database_after"] = database_facts(name)
    return result


def versions() -> dict[str, Any]:
    info: dict[str, Any] = {
        "openminion": getattr(openminion, "__version__", "?"),
        "openminion_file": openminion.__file__,
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
    }
    for mod in ("sqlalchemy", "psycopg"):
        try:
            info[mod] = __import__(mod).__version__
        except Exception as exc:  # noqa: BLE001
            info[mod] = f"unavailable: {type(exc).__name__}"
    try:
        import sqlalchemy as sa

        engine = admin_engine()
        with engine.connect() as conn:
            info["postgres_server"] = conn.execute(sa.text("SHOW server_version")).scalar_one()
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        info["postgres_server"] = f"unavailable: {type(exc).__name__}: {exc}"
    return info


def summary(name: str, result: dict[str, Any]) -> str:
    if "outcome" in result:
        text = result["outcome"]
        if result["outcome"] == "failed":
            text += f" | {result['exception']}"
        else:
            text += f" | record_store={result.get('record_store')} storage_connection={result.get('storage_connection')}"
        if "database_after" in result:
            text += f" | after: {result['database_after']}"
        return f"CASE {name}: {text}"
    if "exit_code" in result:
        last = (result["stderr"].strip().splitlines() or [""])[-1]
        text = f"exit={result['exit_code']} | stderr last line: {last!r}"
        if "database_after" in result:
            text += f" | after: {result['database_after']}"
        return f"CASE {name}: {text}"
    return f"CASE {name}: {json.dumps(result, sort_keys=True)}"


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
    log(f"PROBE apiruntime_pg label={args.label} {json.dumps(report['versions'], sort_keys=True)}")
    steps = (
        ("sqlite_api", lambda: case_sqlite_api(work)),
        ("postgres_api", lambda: case_postgres_api(work, label)),
        ("postgres_env", lambda: case_postgres_env(work, label)),
        ("storage_direct", lambda: case_storage_direct(work, label)),
    )
    for name, step in steps:
        try:
            result = step()
        except Exception as exc:  # noqa: BLE001
            result = {"probe_error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
        report["cases"][name] = result
        log(summary(name, result))
        if result.get("outcome") == "failed":
            log(f"TRACEBACK {name}:\n{result['traceback']}")
    try:
        cli = case_cli(work, label, args.openminion_bin)
    except Exception as exc:  # noqa: BLE001
        cli = {"cli_probe_error": {"probe_error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}}
    for name, result in cli.items():
        report["cases"][name] = result
        log(summary(name, result))
    for name, step in (
        ("postgres_api_after_storage_migrate", lambda: case_migrate_then_api(work, label, args.openminion_bin)),
        ("postgres_api_plain_url", lambda: case_plain_url(work, label)),
    ):
        try:
            result = step()
        except Exception as exc:  # noqa: BLE001
            result = {"probe_error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
        report["cases"][name] = result
        log(summary(name, result))
        if "storage_migrate" in result:
            migrate = result["storage_migrate"]
            log(f"STORAGE MIGRATE exit={migrate['exit_code']} modules={migrate['module_status']}")
        if result.get("outcome") == "failed":
            log(f"TRACEBACK {name}:\n{result['traceback']}")
    Path(args.out).write_text(
        json.dumps(clean_any(report), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
