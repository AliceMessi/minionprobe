"""Helpers for probe_390.py: instance and close() accounting for the classes a
runtime memory assembly build obtains, the sqlite3 connections still open on a
case's files (found without collecting), immediate file operations, the
caller-owned vector adapter and scheduler, and PostgreSQL engine/connection
observation. Installed package only (no repo test fixtures).

probe_390.py disables the cyclic GC and replaces gc.collect with a counter
before anything here (or openminion) is imported; nothing here collects.
"""

from __future__ import annotations

import gc
import hashlib
import os
import shutil
import sqlite3
import time
import traceback
import weakref
from pathlib import Path
from typing import Any

# ------------------------------------------------------------- sqlite3 sites
OPENED: dict[int, str] = {}
_CONNECT = sqlite3.connect


def _site() -> str:
    frames = []
    for frame in traceback.extract_stack()[:-2]:
        name = frame.filename.replace("\\", "/")
        for marker in ("/openminion/", "/sophiagraph/"):
            index = name.rfind(marker)
            if index >= 0:
                frames.append(f"{name[index + 1:]}:{frame.lineno} {frame.name}")
                break
    return " <- ".join(reversed(frames[-4:])) or "(probe code)"


def _tracking_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
    conn = _CONNECT(*args, **kwargs)
    OPENED[id(conn)] = _site()  # an id only: no reference that could keep it open
    return conn


def install_sqlite_tracking() -> None:
    sqlite3.connect = _tracking_connect  # type: ignore[assignment]
    sqlite3.dbapi2.connect = _tracking_connect  # type: ignore[assignment]


def under(path: str, root: Path) -> bool:
    try:
        Path(os.path.normcase(os.path.abspath(path))).relative_to(os.path.normcase(os.path.abspath(str(root))))
        return True
    except ValueError:
        return False


def open_connections(root: Path, *, exclude: tuple[Path, ...] = ()) -> dict[str, Any]:
    """sqlite3 connections still open on files under `root` (gc.get_objects, no collection)."""
    holders, uninspectable = [], 0
    for obj in gc.get_objects():
        if not isinstance(obj, sqlite3.Connection):
            continue
        try:
            files = [row[2] for row in obj.execute("PRAGMA database_list") if row[2]]
        except sqlite3.ProgrammingError:
            continue  # closed
        except Exception:  # noqa: BLE001 - e.g. owned by another thread
            uninspectable += 1
            continue
        for name in files:
            if under(name, root) and not any(under(name, ex) for ex in exclude):
                rel = os.path.relpath(name, root).replace("\\", "/")
                holders.append({"file": rel, "opened at": OPENED.get(id(obj), "(opened before tracking)")})
    return {"count": len(holders), "connections": holders, "uninspectable": uninspectable}


def err(exc: BaseException) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return f"{type(exc).__name__} [WinError {winerror}]"
    errno = getattr(exc, "errno", None)
    if errno is not None:
        return f"{type(exc).__name__} [errno {errno}]"
    return f"{type(exc).__name__}: {exc}"


def attempt(action) -> str:
    try:
        action()
        return "ok"
    except Exception as exc:  # noqa: BLE001 - recorded
        return err(exc)


SQLITE_SUFFIXES = (".db", ".sqlite3", ".sqlite")
COMPANIONS = ("-wal", "-shm", "-journal")


def files_under(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def immediate_file_ops(root: Path, folders: list[Path]) -> dict[str, str]:
    """Rename every SQLite file under `folders` away and back, delete it (and any
    companion), then remove the folders. Nothing is collected first."""
    ops: dict[str, str] = {}
    for folder in folders:
        if not folder.exists() or not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or not path.name.endswith(SQLITE_SUFFIXES):
                continue
            rel = path.relative_to(root).as_posix()
            moved = path.with_name(path.name + ".probe-moved")
            ops[f"rename {rel} away"] = result = attempt(lambda: os.rename(path, moved))
            if result == "ok":
                ops[f"rename {rel} back"] = attempt(lambda: os.rename(moved, path))
            ops[f"delete {rel}"] = attempt(lambda: os.remove(path))
            for suffix in COMPANIONS:
                companion = path.with_name(path.name + suffix)
                if companion.exists():
                    ops[f"delete {rel}{suffix}"] = attempt(lambda: os.remove(companion))
    for folder in folders:
        if folder.exists():
            ops[f"remove folder {folder.relative_to(root).as_posix()}"] = attempt(lambda: shutil.rmtree(folder))
    return ops


# ------------------------------------------------------------- close accounting
class Tracker:
    """Records the instances of tracked classes created while a case is active,
    and every call of their close() (or another method). Instances are held by
    weak reference only, so the probe keeps nothing alive. `raise_after` makes
    the real close run first and then raise, for the named labels."""

    def __init__(self) -> None:
        self.case: str | None = None
        self.records: list[dict[str, Any]] = []
        self.raise_after: set[str] = set()

    def _find(self, obj: Any) -> dict[str, Any] | None:
        for rec in reversed(self.records):
            if rec["id"] == id(obj) and (rec["ref"] is None or rec["ref"]() is obj):
                return rec
        return None

    def track(self, cls: type, label: Any, method: str = "close") -> None:
        tracker = self
        orig_init = cls.__init__
        orig_method = getattr(cls, method)

        def __init__(obj: Any, *args: Any, **kwargs: Any) -> None:
            orig_init(obj, *args, **kwargs)
            if tracker.case is not None and tracker._find(obj) is None:
                name = label(obj, args, kwargs) if callable(label) else label
                try:
                    ref = weakref.ref(obj)
                except TypeError:
                    ref = None
                tracker.records.append({"case": tracker.case, "label": name, "id": id(obj), "ref": ref,
                                        "calls": 0, "method": method})

        def wrapped(obj: Any, *args: Any, **kwargs: Any) -> Any:
            rec = tracker._find(obj)
            if rec is not None:
                rec["calls"] += 1
            result = orig_method(obj, *args, **kwargs)
            if rec is not None and rec["label"] in tracker.raise_after:
                raise OSError(f"probe: {rec['label']}.{method}() raised after closing")
            return result

        cls.__init__ = __init__  # type: ignore[method-assign]
        setattr(cls, method, wrapped)

    def calls(self, case: str) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for rec in self.records:
            if rec["case"] == case:
                out.setdefault(rec["label"], []).append(rec["calls"])
        return out

    def instance(self, case: str, label: str) -> Any:
        for rec in self.records:
            if rec["case"] == case and rec["label"] == label and rec["ref"] is not None:
                return rec["ref"]()
        return None


# ------------------------------------------------------------- borrowed objects
def caller_vector_classes():
    """A deterministic embedding provider and a caller-owned VectorIndexAdapter
    with a close(), over the package's own SQLiteVecBackend."""
    from openminion.modules.storage.runtime.vector_index import (
        EmbeddingBatchResult,
        EmbeddingProvider,
        EmbeddingResult,
        VectorIndexAdapter,
        VectorSpaceIdentity,
    )

    class HashEmbedding(EmbeddingProvider):
        dimension = 8

        def embed(self, text: str) -> EmbeddingResult:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            raw = [b / 255.0 + 0.01 for b in digest[: self.dimension]]
            norm = sum(v * v for v in raw) ** 0.5
            return EmbeddingResult(vector=[v / norm for v in raw], provider="probe-hash", model="sha256-8")

        def embed_batch(self, texts: list[str]) -> EmbeddingBatchResult:
            return EmbeddingBatchResult(results=[self.embed(t) for t in texts])

        @property
        def semantic_ready(self) -> bool:
            return True

        @property
        def identity(self) -> VectorSpaceIdentity:
            return VectorSpaceIdentity(provider="probe-hash", model="sha256-8", dimension=self.dimension)

    class CallerOwnedVectorAdapter(VectorIndexAdapter):
        """The caller's adapter: it owns its SQLiteVecBackend connection."""

        def close(self) -> None:
            self._vector_index.conn.close()

    return HashEmbedding, CallerOwnedVectorAdapter


# ------------------------------------------------------------- PostgreSQL
class Engines:
    """Wraps sqlalchemy.create_engine while a case is active: remembers each
    engine created for the case's application_name and counts its disposals
    (the engine_disposed event)."""

    def __init__(self) -> None:
        import sqlalchemy as sa
        from sqlalchemy import event

        self.sa = sa
        self.event = event
        self.app: str | None = None
        self.engines: list[dict[str, Any]] = []
        self._orig = sa.create_engine
        engines = self

        def create_engine(*args: Any, **kwargs: Any):
            engine = engines._orig(*args, **kwargs)
            url = str(args[0] if args else kwargs.get("url", ""))
            if engines.app and engines.app in url:
                rec = {"app": engines.app, "engine": engine, "disposed": 0}
                event.listen(engine, "engine_disposed", lambda *_a, r=rec: r.__setitem__("disposed", r["disposed"] + 1))
                engines.engines.append(rec)
            return engine

        sa.create_engine = create_engine

    def for_app(self, app: str) -> list[dict[str, Any]]:
        return [rec for rec in self.engines if rec["app"] == app]


def server_connections(admin: Any, app: str) -> int:
    import sqlalchemy as sa

    with admin.connect() as conn:
        return int(conn.execute(sa.text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :a"),
                                {"a": app}).scalar_one())


def settle_connections(admin: Any, app: str, timeout: float = 6.0) -> dict[str, Any]:
    """Poll pg_stat_activity until no backend has `app` (a closed client socket
    takes a moment to end its server process), or the timeout passes."""
    start = time.monotonic()
    polls = 0
    while True:
        polls += 1
        count = server_connections(admin, app)
        elapsed = time.monotonic() - start
        if count == 0 or elapsed >= timeout:
            return {"count": count, "polls": polls, "seconds": round(elapsed, 2)}
        time.sleep(0.2)
