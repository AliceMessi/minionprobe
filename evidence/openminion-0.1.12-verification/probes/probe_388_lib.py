"""Helpers shared by probe_388.py, probe_389.py and probe_397.py (installed package only).

- an echo runtime config plus a ConfigManager with a registered memory config
  (the way upstream's runtime-memory tests select a memory backend);
- external backends built on the bundled reference engine
  (sophiagraph's SophiaGraphSqliteStore): one with a counting close(), one
  with the optional apply_capture_bundle, one whose apply_capture_bundle fails;
- a scheduler stub whose liveness the probe controls;
- per-object close counters patched onto classes;
- a PostgreSQL schema + application_name per case, with server connection counts
  from pg_stat_activity (the URL is never printed).
"""

from __future__ import annotations

import gc
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from _common import echo_config

from openminion.base.config import ConfigManager, OpenMinionConfig, save_config


# ---------------------------------------------------------------- GC accounting
class GcGuard:
    """Count gc.collect() calls; the probe calls gc.disable() before importing openminion."""

    def __init__(self) -> None:
        self.calls = 0
        self._orig = gc.collect

        def counting_collect(*args: Any, **kwargs: Any) -> int:
            self.calls += 1
            return self._orig(*args, **kwargs)

        gc.collect = counting_collect  # type: ignore[assignment]

    def state(self) -> dict[str, Any]:
        return {"gc_enabled": gc.isenabled(), "gc_collect_calls": self.calls}


# ---------------------------------------------------------------- config
def runtime_manager(root: Path, memory_config: Any = None) -> ConfigManager:
    """Echo runtime config (explicit storage path under root) + ConfigManager.

    memory_config: None (default memory config), a dict, or a callable(**kwargs).
    """
    path = echo_config(root)
    manager = ConfigManager.load(str(path), home_root=root, data_root=root / ".openminion")
    if memory_config is not None:
        fn = memory_config if callable(memory_config) else (lambda **_kwargs: memory_config)
        manager.register("memory", fn)
    return manager


def fallback_config(agent: str = "main") -> OpenMinionConfig:
    """Config the bootstrap fallback tests use: echo agent, memory_v2 enabled."""
    from openminion.base.config import AgentProfileConfig

    config = OpenMinionConfig()
    config.agents = {agent: AgentProfileConfig(name=agent, provider="echo", default_channel="console")}
    config.default_agent = agent
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"
    return config


def fallback_manager(root: Path, config: OpenMinionConfig, memory_config: Any = None) -> ConfigManager:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "agents.json"
    save_config(config, str(path))
    manager = ConfigManager.load(str(path), home_root=root, data_root=root / "data")
    if memory_config is not None:
        manager.register("memory", lambda **_kwargs: memory_config)
    return manager


def external_config(db_path: Path, adapter: str, store: dict | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "backend": {"provider": "external", "external_adapter": adapter, "options": {"db_path": str(db_path)}}
    }
    if store is not None:
        out["store"] = store
    return out


# ---------------------------------------------------------------- external backends
CLOSING_ADAPTER = "probe-closing-reference-sqlite"
CAPTURING_ADAPTER = "probe-capturing-reference-sqlite"
FAILING_ADAPTER = "probe-failing-reference-sqlite"
BACKEND_CLOSES: list[int] = []
CAPTURED_BUNDLES: list[Any] = []


class CaptureFailed(RuntimeError):
    """The error the failing backend's own apply_capture_bundle raises."""


def register_backends() -> dict[str, type]:
    from sophiagraph.storage import SophiaGraphSqliteStore

    from openminion.modules.memory.backends.external import (
        ExternalBackendCapabilities,
        register_external_backend,
        register_reference_sqlite_backend,
    )

    class ClosingReferenceBackend(SophiaGraphSqliteStore):
        """The reference engine plus a close() that records each call."""

        def close(self) -> None:
            BACKEND_CLOSES.append(id(self))

    class CapturingReferenceBackend(SophiaGraphSqliteStore):
        """The reference engine plus apply_capture_bundle, applied by the installed
        built-in SQLite capture implementation in a store next to the backend DB."""

        def __init__(self, path: Path) -> None:
            super().__init__(path)
            from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore

            self.capture_store = SQLiteMemoryStore(Path(path).with_suffix(".capture.db"))
            self.bundles: list[Any] = []

        def apply_capture_bundle(self, bundle: Any) -> Any:
            self.bundles.append(bundle)
            CAPTURED_BUNDLES.append(bundle)
            return self.capture_store.apply_capture_bundle(bundle)

        def close(self) -> None:
            BACKEND_CLOSES.append(id(self))
            self.capture_store.close()

    class FailingReferenceBackend(SophiaGraphSqliteStore):
        def apply_capture_bundle(self, bundle: Any) -> Any:
            raise CaptureFailed("backend capture failed")

    register_reference_sqlite_backend()
    for name, cls in ((CLOSING_ADAPTER, ClosingReferenceBackend), (CAPTURING_ADAPTER, CapturingReferenceBackend),
                      (FAILING_ADAPTER, FailingReferenceBackend)):

        def factory(*, config: Any, _cls: type = cls, **_: Any) -> Any:
            return _cls(Path(str(config.options["db_path"])))

        register_external_backend(name, factory=factory, capabilities=ExternalBackendCapabilities())
    return {"closing": ClosingReferenceBackend, "capturing": CapturingReferenceBackend,
            "failing": FailingReferenceBackend, "reference": SophiaGraphSqliteStore}


# ---------------------------------------------------------------- scheduler stub
class Scheduler:
    def __init__(self, *, alive: bool) -> None:
        self.alive = alive
        self.events: list[str] = []

    def bind_record_source(self, record_source: Any) -> None:
        self.events.append("bind")

    def start(self) -> None:
        self.events.append("start")

    def stop(self) -> None:
        self.events.append("stop")

    def is_alive(self) -> bool:
        return self.alive


# ---------------------------------------------------------------- close counters
class CloseCounter:
    """Patch cls.close on classes to record id(obj) per call; restore with undo()."""

    def __init__(self) -> None:
        self.closed: list[int] = []
        self._undo: list[tuple[Any, str, Any]] = []

    def count(self, cls: type) -> None:
        if any(target is cls for target, _, _ in self._undo):
            return
        original = cls.__dict__.get("close", None)
        inherited = cls.close

        def close(obj: Any, *args: Any, **kwargs: Any) -> Any:
            self.closed.append(id(obj))
            return inherited(obj, *args, **kwargs)

        self._undo.append((cls, "close", original))
        cls.close = close  # type: ignore[method-assign]

    def patch(self, target: Any, name: str, value: Any) -> None:
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def times(self, obj: Any) -> int:
        return self.closed.count(id(obj))

    def undo(self) -> None:
        for target, name, value in reversed(self._undo):
            if value is None and name == "close" and isinstance(target, type):
                delattr(target, "close")
            else:
                setattr(target, name, value)
        self._undo.clear()


def write_outcome(action) -> str:
    try:
        action()
        return "accepted"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def rename_delete(path: Path) -> str:
    """Rename then delete a file right away (Windows: fails while a handle is open)."""
    import os

    try:
        moved = path.with_name(path.name + ".moved")
        os.replace(path, moved)
        os.remove(moved)
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- PostgreSQL
class PgDatabase:
    """One schema and application_name; counts server connections for it."""

    def __init__(self, admin: Any, base_url: str, label: str) -> None:
        import sqlalchemy as sa

        self.sa = sa
        self.admin = admin
        token = uuid.uuid4().hex[:10]
        self.schema = f"probe_{label}_{token}"
        self.app = f"probe_{label}_{token}"
        with admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{self.schema}"'))
        self.url = f"{base_url}?options={quote_plus('-csearch_path=' + self.schema)}&application_name={self.app}"

    def connections(self) -> int:
        with self.admin.connect() as conn:
            return int(conn.execute(self.sa.text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :a"),
                                    {"a": self.app}).scalar_one())

    def settled(self) -> int:
        deadline = time.monotonic() + 5.0
        count = self.connections()
        while count and time.monotonic() < deadline:
            time.sleep(0.05)
            count = self.connections()
        return count

    def tables(self) -> int:
        with self.admin.connect() as conn:
            return int(conn.execute(self.sa.text("SELECT count(*) FROM information_schema.tables WHERE table_schema = :s"),
                                    {"s": self.schema}).scalar_one())

    def drop(self) -> None:
        with self.admin.begin() as conn:
            conn.execute(self.sa.text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
