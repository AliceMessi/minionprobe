"""Helpers for the #437, #364 and #369 probes on the installed 0.1.15
(APIRuntime on PostgreSQL runtime storage). Installed package only.

PostgreSQL: each case gets a new, empty database on the server named by
PGURL (or OPENMINION_TEST_POSTGRES_URL), and a URL that names its own
`application_name`. The probe counts that application_name in
pg_stat_activity (server side) while the runtime or CLI is running and after
it closed or exited. Its own reads use other application names.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

STRIP_ENV = (
    "OPENMINION_HOME",
    "OPENMINION_DATA_ROOT",
    "OPENMINION_GENERATED_ROOT",
    "OPENMINION_CONFIG_PATH",
    "OPENMINION_STORAGE_BACKEND",
    "OPENMINION_STORAGE_POSTGRES_URL",
    "OPENMINION_STORAGE_POSTGRES_POOL_MIN",
    "OPENMINION_STORAGE_POSTGRES_POOL_MAX",
)
RUNTIME_ERROR = "runtime record_store does not expose sqlite connection"
_PW = re.compile(r"(postgres(?:ql)?(?:\+\w+)?://[^:/@\s]+):[^@\s]*@")


def mask(text: str) -> str:
    """The runner's PostgreSQL password is public, but keep it out of the evidence."""
    return _PW.sub(r"\1:***@", text)


class MaskingStream(io.TextIOBase):
    def __init__(self, inner) -> None:
        self._inner = inner

    def write(self, text: str) -> int:
        self._inner.write(mask(text))
        return len(text)

    def flush(self) -> None:
        self._inner.flush()


def install_masking() -> None:
    sys.stdout = MaskingStream(sys.stdout)  # type: ignore[assignment]
    sys.stderr = MaskingStream(sys.stderr)  # type: ignore[assignment]


def strip_env() -> None:
    for key in STRIP_ENV:
        os.environ.pop(key, None)


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra or {})
    return env


# ---------------------------------------------------------------- PostgreSQL
def pg_base() -> str:
    return (os.environ.get("PGURL") or os.environ.get("OPENMINION_TEST_POSTGRES_URL") or "").strip()


def _with(url: str, *, database: str | None = None, app: str | None = None, scheme: str | None = None) -> str:
    parts = urlsplit(url)
    if database is not None:
        parts = parts._replace(path=f"/{database}")
    if app is not None:
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "application_name"]
        query.append(("application_name", app))
        parts = parts._replace(query=urlencode(query))
    if scheme is not None:
        parts = parts._replace(scheme=scheme)
    return urlunsplit(parts)


def _engine(url: str):
    import sqlalchemy as sa
    from sqlalchemy.pool import NullPool

    return sa.create_engine(url, poolclass=NullPool, future=True)


def pg_versions() -> dict[str, Any]:
    import sqlalchemy as sa

    info: dict[str, Any] = {"sqlalchemy": sa.__version__}
    try:
        import psycopg

        info["psycopg"] = psycopg.__version__
    except Exception as exc:  # noqa: BLE001
        info["psycopg"] = f"unavailable: {type(exc).__name__}"
    engine = _engine(_with(pg_base(), database="postgres", app="probe_admin"))
    try:
        with engine.connect() as conn:
            info["select version()"] = conn.execute(sa.text("select version()")).scalar_one()
            info["server_version"] = conn.execute(sa.text("show server_version")).scalar_one()
    finally:
        engine.dispose()
    info["url_scheme"] = urlsplit(pg_base()).scheme
    return info


class PgDatabase:
    """A new database with its own application_name (= its name)."""

    def __init__(self, prefix: str) -> None:
        import sqlalchemy as sa

        self.name = f"{prefix}_{uuid.uuid4().hex[:8]}".lower()
        self.app = self.name
        engine = _engine(_with(pg_base(), database="postgres", app="probe_admin"))
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(sa.text(f'CREATE DATABASE "{self.name}"'))
        finally:
            engine.dispose()

    def url(self, scheme: str | None = None, app: str | None = None) -> str:
        """The runtime's URL: this database and its application_name (or `app`)."""
        return _with(pg_base(), database=self.name, app=app or self.app, scheme=scheme)

    def rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        """Rows read through an independent connection (application_name probe_reader)."""
        import sqlalchemy as sa

        engine = _engine(_with(pg_base(), database=self.name, app="probe_reader"))
        try:
            with engine.connect() as conn:
                return [dict(r._mapping) for r in conn.execute(sa.text(sql), params)]
        finally:
            engine.dispose()

    def tables(self) -> list[str]:
        return [r["table_name"] for r in self.rows(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1")]

    def connections_now(self) -> int:
        import sqlalchemy as sa

        engine = _engine(_with(pg_base(), database="postgres", app="probe_admin"))
        try:
            with engine.connect() as conn:
                return int(conn.execute(
                    sa.text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :a"),
                    {"a": self.app},
                ).scalar_one())
        finally:
            engine.dispose()

    def connections_settled(self, timeout: float = 5.0) -> dict[str, Any]:
        """Count this application_name's server connections, waiting up to `timeout`
        seconds for a disconnect to reach pg_stat_activity (no gc.collect)."""
        start = time.monotonic()
        while True:
            count = self.connections_now()
            waited = round(time.monotonic() - start, 2)
            if count == 0 or waited >= timeout:
                return {"count": count, "waited_s": waited}
            time.sleep(0.05)

    def drop(self) -> None:
        import sqlalchemy as sa

        engine = _engine(_with(pg_base(), database="postgres", app="probe_admin"))
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))
        finally:
            engine.dispose()


class ConnectionWatch:
    """Polls pg_stat_activity for one application_name while a subprocess runs."""

    def __init__(self, db: PgDatabase | None) -> None:
        self.db = db
        self.max = 0
        self.samples = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "ConnectionWatch":
        if self.db is not None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.max = max(self.max, self.db.connections_now())  # type: ignore[union-attr]
                self.samples += 1
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(0.05)

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)


# ---------------------------------------------------------------- configs and files
def write_config(root: Path, *, agents: tuple[str, ...] = ("first",), storage: dict[str, Any] | None = None,
                 default_agent: str | None = None, filename: str = "agents.json",
                 runtime_extra: dict[str, Any] | None = None) -> Path:
    """A hand-written config file: offline `echo` agents, single process, explicit storage.path."""
    config = {
        "default_agent": default_agent or agents[0],
        "agents": {name: {"provider": "echo", "default_channel": "console"} for name in agents},
        "runtime": {"process_mode": "single-process", "daemon_auto_start": False, **(runtime_extra or {})},
        "storage": {"path": str(root / ".openminion" / "state.db"), **(storage or {})},
    }
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def storage_path(root: Path) -> Path:
    return root / ".openminion" / "state.db"


def sqlite_files(root: Path) -> dict[str, Any]:
    """SQLite files under `root`: table count, and the runtime ledger/sessions rows if present."""
    found: dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        try:
            if not path.is_file() or path.read_bytes()[:16] != b"SQLite format 3\x00":
                continue
        except OSError:
            continue
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            info: dict[str, Any] = {"tables": len(tables)}
            if "sessions" in tables:
                info["sessions_rows"] = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
            if "room_participants" in tables:
                info["room_participants_rows"] = conn.execute("SELECT count(*) FROM room_participants").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            info = {"error": str(exc)}
        finally:
            conn.close()
        found[path.relative_to(root).as_posix()] = info
    return found


def sqlite_rows(db: Path, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Rows read through an independent stdlib sqlite3 connection."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


class Reader:
    """Independent reads of the runtime database (PostgreSQL or the SQLite file),
    with `?` placeholders."""

    def __init__(self, *, db: PgDatabase | None = None, sqlite_path: Path | None = None) -> None:
        self.db = db
        self.sqlite_path = sqlite_path

    def rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        if self.db is not None:
            named = {}
            text = sql
            for i, value in enumerate(params):
                text = text.replace("?", f":p{i}", 1)
                named[f"p{i}"] = value
            return self.db.rows(text, **named)
        return sqlite_rows(self.sqlite_path, sql, params)  # type: ignore[arg-type]


# ---------------------------------------------------------------- CLI subprocesses
def openminion_exe() -> str:
    name = "openminion.exe" if os.name == "nt" else "openminion"
    return str(Path(sys.executable).with_name(name))


def run_process(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None, watch: PgDatabase | None = None,
                timeout: float = 600) -> dict[str, Any]:
    start = time.monotonic()
    with ConnectionWatch(watch) as w:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env if env is not None else clean_env(), cwd=str(cwd), timeout=timeout, check=False,
        )
    result: dict[str, Any] = {
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr_tail": [ln for ln in proc.stderr.strip().splitlines() if ln.strip()][-6:],
        "seconds": round(time.monotonic() - start, 2),
    }
    if watch is not None:
        result["connections_max_while_running"] = w.max
        result["connections_samples"] = w.samples
        result["connections_after_exit"] = watch.connections_settled()
    return result


def cli_argv(root: Path, cfg: Path, argv: list[str]) -> list[str]:
    return ["--home-root", str(root), "--data-root", str(root / ".openminion"), "--config", str(cfg), *argv]


def run_cli(root: Path, cfg: Path, argv: list[str], *, watch: PgDatabase | None = None,
            env_extra: dict[str, str] | None = None) -> dict[str, Any]:
    """The installed `openminion` console script as a subprocess."""
    full = cli_argv(root, cfg, argv)
    result = run_process([openminion_exe(), *full], cwd=root, env=clean_env(env_extra), watch=watch)
    result["argv"] = ["openminion", *full]
    return result


def kv(stdout: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)


def brief(result: dict[str, Any]) -> dict[str, Any]:
    keep = {k: v for k, v in result.items() if k not in ("stdout", "argv")}
    keep["stdout"] = result.get("stdout", "").strip().splitlines()[:12]
    return keep


def describe_exc(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"
