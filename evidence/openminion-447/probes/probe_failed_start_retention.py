"""What an in-process APIRuntime start that fails leaves open (installed package only).

    python -I -X utf8 probe_failed_start_retention.py --label <name> --work <dir> --out <out.json>

Every case runs in its own child process (the same file with --child <case>), so
threads and handles from one case cannot show up in another. The child disables
automatic garbage collection before it imports openminion, so the first snapshot
("at_failure") only reflects reference counting; the second one is taken after an
explicit gc.collect(). Cases:

  sqlite_started_then_closed   control: SQLite runtime storage, the start succeeds,
                               runtime.close()
  sqlite_memory_provider       SQLite runtime storage, OPENMINION_MEMORY_PROVIDER=not-a-provider:
                               the start raises ConfigError while building the memory
                               assembly (after the runtime storage was built)
  sqlite_437_point             SQLite runtime storage, RuntimeStorageContext.connection
                               patched to raise the #437 RuntimeError: the start fails at
                               the same point as on PostgreSQL (fault injection)
  postgres_437                 PostgreSQL runtime storage (needs PGURL): the #437 RuntimeError
  postgres_runtime_migration   PostgreSQL runtime storage on a database that already has a
                               `sessions` table: MigrationError inside build_runtime_storage
  postgres_storage_closed      control: build_runtime_storage on PostgreSQL, then close()

Each snapshot records: files under the case root that this process holds open
(psutil), which of them cannot be renamed right now (Windows), threads started by
the attempt that are still alive, whether the RuntimeStorageContext built by the
attempt is still alive and how often its close() ran, and, for PostgreSQL, how many
other connections to the case database are open. If the context is still alive at
the end, the probe calls its own close() once and takes a last snapshot.
"""

from __future__ import annotations

import gc

gc.disable()  # before openminion is imported; gc.collect() is called explicitly below

import argparse
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import threading
import traceback
import weakref
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

PGURL = os.environ.get("PGURL", "")
STRIP_ENV = (
    "OPENMINION_HOME",
    "OPENMINION_DATA_ROOT",
    "OPENMINION_GENERATED_ROOT",
    "OPENMINION_CONFIG_PATH",
    "OPENMINION_STORAGE_BACKEND",
    "OPENMINION_STORAGE_POSTGRES_URL",
    "OPENMINION_MEMORY_PROVIDER",
)
SQLITE_CASES = ("sqlite_started_then_closed", "sqlite_memory_provider", "sqlite_437_point")
PG_CASES = ("postgres_437", "postgres_runtime_migration", "postgres_storage_closed")
EXPECTED = {
    "sqlite_started_then_closed": None,
    "sqlite_memory_provider": "ConfigError",
    "sqlite_437_point": "RuntimeError",
    "postgres_437": "RuntimeError",
    "postgres_runtime_migration": "MigrationError",
    "postgres_storage_closed": None,
}


# ---------------------------------------------------------------- sanitizing
def _prefixes(work: str | None) -> list[tuple[str, str]]:
    pairs = []
    for value, token in (
        (work, "<WORK>"),
        (sys.prefix, "<VENV>"),
        (os.environ.get("RUNNER_TEMP"), "<RUNNER_TEMP>"),
        (str(Path.home()), "<USER_HOME>"),
    ):
        if value:
            for form in {str(Path(value)), str(Path(value).resolve())}:
                pairs.append((form, token))
                pairs.append((form.replace("\\", "/"), token))
    return sorted(set(pairs), key=lambda item: len(item[0]), reverse=True)


PREFIXES: list[tuple[str, str]] = _prefixes(None)


def clean(text: str) -> str:
    for raw, token in PREFIXES:
        text = text.replace(raw, token)
    return re.sub(r"(postgresql(?:\+\w+)?://[^:/@\s]+):[^@\s]*@", r"\1:***@", text)


def clean_any(value: Any) -> Any:
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, dict):
        return {k: clean_any(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_any(v) for v in value]
    return value


# ---------------------------------------------------------------- PostgreSQL
def db_url(name: str) -> str:
    parts = urlsplit(PGURL)
    return urlunsplit(parts._replace(path=f"/{name}"))


def create_database(name: str) -> str:
    import sqlalchemy as sa

    engine = sa.create_engine(db_url("postgres"), isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    return db_url(name)


def pg_other_connections(name: str) -> int:
    import sqlalchemy as sa

    engine = sa.create_engine(db_url(name), future=True)
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = :db AND pid <> pg_backend_pid()"
                    ),
                    {"db": name},
                ).scalar_one()
            )
    finally:
        engine.dispose()


def pg_run(name: str, sql: str) -> None:
    import sqlalchemy as sa

    engine = sa.create_engine(db_url(name), future=True)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(sql))
    finally:
        engine.dispose()


# ---------------------------------------------------------------- config
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


# ---------------------------------------------------------------- child
class Tracker:
    """Counts RuntimeStorageContext.close() and close_runtime_components() calls and
    keeps a weak reference to every RuntimeStorageContext the attempt builds."""

    def __init__(self) -> None:
        from openminion.api.core import infrastructure, lifecycle
        from openminion.modules.storage.runtime.context import RuntimeStorageContext

        self.contexts: list[weakref.ref] = []
        self.engines: list[weakref.ref] = []
        self.record_stores: list[weakref.ref] = []
        self.close_calls: dict[int, int] = {}
        self.component_close_calls = 0
        original_close = RuntimeStorageContext.close
        tracker = self

        def counting_close(ctx: Any) -> None:
            tracker.close_calls[id(ctx)] = tracker.close_calls.get(id(ctx), 0) + 1
            return original_close(ctx)

        RuntimeStorageContext.close = counting_close  # type: ignore[method-assign]
        original_build = infrastructure.build_runtime_storage

        def recording_build(*args: Any, **kwargs: Any) -> Any:
            ctx = original_build(*args, **kwargs)
            tracker.track(ctx)
            return ctx

        infrastructure.build_runtime_storage = recording_build
        original_components = lifecycle.close_runtime_components

        def counting_components(**kwargs: Any) -> None:
            tracker.component_close_calls += 1
            return original_components(**kwargs)

        lifecycle.close_runtime_components = counting_components

    def track(self, ctx: Any) -> None:
        self.contexts.append(weakref.ref(ctx))
        self.record_stores.append(weakref.ref(ctx.record_store))
        if getattr(ctx, "engine", None) is not None:
            self.engines.append(weakref.ref(ctx.engine))

    @staticmethod
    def _first(refs: list[weakref.ref]) -> Any:
        for ref in refs:
            obj = ref()
            if obj is not None:
                return obj
        return None

    def context(self) -> Any:
        return self._first(self.contexts)

    def engine(self) -> Any:
        return self._first(self.engines)

    def record_store(self) -> Any:
        return self._first(self.record_stores)


def open_files_under(root: Path) -> list[str]:
    import psutil

    prefix = str(root.resolve()).lower()
    found = set()
    for item in psutil.Process().open_files():
        path = str(Path(item.path))
        if path.lower().startswith(prefix):
            found.add(Path(path).resolve().relative_to(root.resolve()).as_posix())
    return sorted(found)


def locked_files_under(root: Path) -> list[str] | str:
    """Files under root that cannot be renamed now (only meaningful on Windows)."""
    if os.name != "nt":
        return "not checked (POSIX allows renaming open files)"
    locked = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        moved = path.with_name(path.name + ".probe-moved")
        try:
            os.replace(path, moved)
        except OSError as exc:
            locked.append(f"{path.relative_to(root).as_posix()}: {type(exc).__name__} [WinError {getattr(exc, 'winerror', '?')}]")
            continue
        os.replace(moved, path)
    return locked


def context_state(tracker: Tracker) -> dict[str, Any]:
    """The RuntimeStorageContext the attempt built, its StorageEngine and record store."""
    here = sys._getframe()
    ctx, engine, store = tracker.context(), tracker.engine(), tracker.record_store()
    state: dict[str, Any] = {
        "contexts_built": len(tracker.contexts),
        "context_alive": ctx is not None,
        "storage_engine_alive": engine is not None,
        "record_store_alive": store is not None,
        "context_close_calls": sum(tracker.close_calls.values()),
        "close_runtime_components_calls": tracker.component_close_calls,
    }
    if store is not None:
        state["record_store"] = type(store).__name__
        conn = getattr(store, "_conn", None)
        if isinstance(conn, sqlite3.Connection):
            try:
                conn.execute("SELECT 1").fetchone()
                state["sqlite_connection_open"] = True
            except sqlite3.ProgrammingError:
                state["sqlite_connection_open"] = False
        state["record_store_referrers"] = sorted(
            type(r).__name__ for r in gc.get_referrers(store) if r is not here
        )
        conn = None
    if engine is not None:
        emitter = getattr(engine, "_pool_health_emitter", None)
        state["pool_health_emitter_running"] = bool(emitter is not None and emitter.is_running)
        emitter = None
    if ctx is not None:
        state["context_closed_flag"] = bool(getattr(ctx, "_closed", False))
    del ctx, engine, store
    return state


def memory_registry_state() -> dict[str, Any]:
    """The module-level memory backend factory registry and the artifact stores alive in gc."""
    from openminion.modules.artifact.control import ArtifactCtl
    from openminion.modules.memory.backends import factory

    registered = factory._BACKEND_FACTORIES.get("sophiagraph")
    cells = [cell.cell_contents for cell in getattr(registered, "__closure__", None) or ()]
    return {
        "memory_backend_registry_sophiagraph_closes_over": sorted(type(c).__name__ for c in cells),
        "artifact_ctl_instances_alive": sum(1 for o in gc.get_objects() if isinstance(o, ArtifactCtl)),
    }


def snapshot(root: Path, baseline_threads: set[int], tracker: Tracker, pg_db: str | None) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "open_files_under_root": open_files_under(root),
        "not_renamable_under_root": locked_files_under(root),
        "new_threads_alive": sorted(
            t.name for t in threading.enumerate() if t.ident not in baseline_threads
        ),
        **context_state(tracker),
        **memory_registry_state(),
    }
    if pg_db:
        snap["pg_other_connections"] = pg_other_connections(pg_db)
    return snap


def raised_at(tb: Any) -> str:
    frames = [f for f in traceback.extract_tb(tb) if "openminion" in f.filename.replace("\\", "/")]
    if not frames:
        return "?"
    last = frames[-1]
    rel = last.filename.replace("\\", "/").split("site-packages/")[-1]
    return f"{rel}:{last.lineno} in {last.name}"


def run_child(case: str, work: Path, label: str) -> dict[str, Any]:
    root = work
    root.mkdir(parents=True, exist_ok=True)
    for key in STRIP_ENV:
        os.environ.pop(key, None)
    baseline_threads = {t.ident for t in threading.enumerate()}

    import openminion
    from openminion import APIRuntime

    tracker = Tracker()
    result: dict[str, Any] = {"case": case, "openminion": getattr(openminion, "__version__", "?")}
    pg_db = None
    storage: dict[str, Any] = {}
    if case.startswith("postgres"):
        pg_db = f"om_{label}_{case}"[:60]
        url = create_database(pg_db)
        storage = {"backend": "postgres", "postgres_url": url}
        if case == "postgres_runtime_migration":
            pg_run(pg_db, "CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
            result["database_setup"] = "CREATE TABLE sessions (session_id TEXT PRIMARY KEY)"
        result["pg_other_connections_before"] = pg_other_connections(pg_db)
    cfg = write_config(root, storage)
    result["config"] = json.loads(cfg.read_text(encoding="utf-8"))
    if case == "sqlite_memory_provider":
        os.environ["OPENMINION_MEMORY_PROVIDER"] = "not-a-provider"
        result["env"] = {"OPENMINION_MEMORY_PROVIDER": "not-a-provider"}
    if case == "sqlite_437_point":
        from openminion.modules.storage.runtime.context import RuntimeStorageContext

        def _raise(self: Any) -> Any:
            raise RuntimeError("runtime record_store does not expose sqlite connection")

        RuntimeStorageContext.connection = property(_raise)  # type: ignore[assignment]
        result["fault_injection"] = (
            "RuntimeStorageContext.connection raises the #437 RuntimeError "
            "(api/core/bootstrap.py:126 reads it)"
        )
    roots = {"home_root": root, "data_root": root / ".openminion"}
    started = False
    if case == "postgres_storage_closed":
        from openminion.base.config.parser import openminion_config_from_dict
        from openminion.modules.storage.runtime.context import build_runtime_storage
        from openminion.modules.telemetry import storage_hook
        from openminion.modules.telemetry.service import TelemetryService

        config = openminion_config_from_dict(json.loads(cfg.read_text(encoding="utf-8")))
        ctx = build_runtime_storage(
            config.storage.path,
            env={},
            record_backend=config.storage.record_backend(),
            record_backend_options=config.storage.record_backend_options(),
            telemetry_hook=storage_hook.TelemetryServiceStorageHook(TelemetryService(home_root=root)),
        )
        tracker.track(ctx)
        result["outcome"] = f"built {type(ctx.record_store).__name__}, migration v{ctx.migration_result.current_version}"
        result["pg_other_connections_while_open"] = pg_other_connections(pg_db)
        ctx.close()
        del ctx
        started = True
    else:
        try:
            runtime = APIRuntime.from_config_path(str(cfg), **roots)
        except Exception as exc:  # noqa: BLE001
            result["outcome"] = "failed"
            result["exception"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
            result["raised_at"] = raised_at(exc.__traceback__)
            result["exception_type_as_expected"] = type(exc).__name__ == EXPECTED[case]
        else:
            started = True
            result["outcome"] = "started"
            runtime.close()
            del runtime
    result["at_failure" if not started else "after_close"] = snapshot(root, baseline_threads, tracker, pg_db)
    gc.collect()
    result["after_gc_collect"] = snapshot(root, baseline_threads, tracker, pg_db)
    ctx, engine, store = tracker.context(), tracker.engine(), tracker.record_store()
    if ctx is not None and getattr(ctx, "_closed", False):
        ctx = engine = store = None  # already closed by the runtime (control)
    emitters = []
    for thread in threading.enumerate():
        if thread.ident in baseline_threads or thread.name != "storage-pool-health-emitter":
            continue
        emitter = getattr(getattr(thread, "_target", None), "__self__", None)
        if emitter is not None:
            emitters.append(emitter)
        emitter = None
    if ctx is not None or engine is not None or store is not None or emitters:
        # Cleanup by the probe, not runtime behaviour: the existing close() of what the
        # attempt built (or of what a leftover pool-health emitter thread still holds),
        # to show that it is what holds the resources above.
        steps = []
        if ctx is not None:
            ctx.close()
            steps.append("RuntimeStorageContext.close()")
        elif engine is not None:
            engine.close()
            steps.append("StorageEngine.close()")
        for emitter in emitters:
            if emitter.is_running:
                emitter.stop()
                steps.append("PoolHealthEmitter.stop() on the leftover emitter thread")
            held = getattr(emitter, "_record_store", None)
            if held is not None and held is not store:
                held.close()
                steps.append(f"{type(held).__name__}.close() on the record store that emitter held")
            held = None
        emitter = None
        if store is not None and ctx is None and engine is None:
            store.close()
            steps.append(f"{type(store).__name__}.close()")
        result["probe_cleanup"] = steps
        ctx = engine = store = None
        emitters = []
        gc.collect()
        result["after_probe_cleanup"] = snapshot(root, baseline_threads, tracker, pg_db)
    return result


# ---------------------------------------------------------------- driver
def versions() -> dict[str, Any]:
    import openminion

    info: dict[str, Any] = {
        "openminion": getattr(openminion, "__version__", "?"),
        "openminion_file": openminion.__file__,
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
    }
    for mod in ("sqlalchemy", "psycopg", "psutil"):
        try:
            info[mod] = __import__(mod).__version__
        except Exception as exc:  # noqa: BLE001
            info[mod] = f"unavailable: {type(exc).__name__}"
    if PGURL:
        try:
            import sqlalchemy as sa

            engine = sa.create_engine(db_url("postgres"), future=True)
            with engine.connect() as conn:
                info["postgres_server"] = conn.execute(sa.text("SHOW server_version")).scalar_one()
            engine.dispose()
        except Exception as exc:  # noqa: BLE001
            info["postgres_server"] = f"unavailable: {type(exc).__name__}: {exc}"
    return info


def line(case: str, result: dict[str, Any]) -> str:
    first = result.get("at_failure") or result.get("after_close") or {}
    after = result.get("after_gc_collect") or {}
    keys = ("open_files_under_root", "not_renamable_under_root", "new_threads_alive",
            "context_alive", "context_close_calls", "pg_other_connections")
    pick = lambda snap: {k: snap[k] for k in keys if k in snap}  # noqa: E731
    head = result.get("exception") or result.get("outcome") or result.get("probe_error")
    return f"CASE {case}: {head} | raised_at={result.get('raised_at', '-')} | first={pick(first)} | after_gc={pick(after)}"


def main() -> int:
    global PREFIXES
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--child", default=None)
    ap.add_argument("--cases", default="")
    args = ap.parse_args()
    work = Path(args.work).resolve()
    PREFIXES = _prefixes(str(work.parent if args.child else work))
    label = re.sub(r"[^a-z0-9]", "_", args.label.lower())
    if args.child:
        try:
            result = run_child(args.child, work, label)
        except Exception as exc:  # noqa: BLE001
            result = {"case": args.child, "probe_error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
        Path(args.out).write_text(json.dumps(clean_any(result), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        return 0
    work.mkdir(parents=True, exist_ok=True)
    cases = [c for c in args.cases.split(",") if c] or list(SQLITE_CASES) + (list(PG_CASES) if PGURL else [])
    report: dict[str, Any] = {"label": args.label, "versions": versions(), "gc": "automatic gc disabled in each child before importing openminion", "cases": {}}
    print(clean(f"PROBE failed_start_retention label={args.label} {json.dumps(clean_any(report['versions']), sort_keys=True)}"), flush=True)
    for case in cases:
        out = work / f"{case}.json"
        proc = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve()), "--label", args.label,
             "--work", str(work / case), "--out", str(out), "--child", case],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
        )
        try:
            result = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            result = {"probe_error": f"child produced no JSON: {exc}"}
        result["child_exit_code"] = proc.returncode
        result["child_stderr_tail"] = clean(proc.stderr[-3000:])
        report["cases"][case] = result
        print(clean(line(case, result)), flush=True)
    Path(args.out).write_text(json.dumps(clean_any(report), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
