"""More helpers for probe_449*.py and probe_456*.py (installed package only, no repo test fixtures).

probe_449_lib.py is the harness of the original reports, byte-identical to
`probe_420_lib.py` in evidence/openminion-449, openminion-unsupported-provider-
extraction and openminion-consolidation-self-capture (same sha256). This file
adds, on top of it:

- StepRecorder: call-through recorders for the steps of pending-capture
  recovery, per intent and in order: the identity check
  (`build_capture_identity` as `_recover_capture` calls it), the authorization
  (`memory_capture_recovery_allowed`, which the cron executor binds into its
  `authorize` callable), the capability query (`memctl.supports_capture_bundles()`,
  when the installed package has it), the source read (`_capture_source_body`),
  the extraction (the agent service's capture candidate extractor), the bundle
  write (memctl, memory service and store `apply_capture_bundle`) and the result
  write (`sessions.commit_capture_result_and_release_hold`). Every recorder
  calls the original and returns or raises exactly what it did.
- readers for the turn.outcome capture intents, their memory.capture.result
  events and the turn diagnostics that the runtime keeps in message metadata;
- a real store write failure: a BEFORE INSERT trigger on the memory store's
  `memory_capture_bundle_receipts` table that makes the database refuse the
  insert (SQLite `RAISE(ABORT, ...)`, PostgreSQL `RAISE EXCEPTION`), created and
  dropped through a separate connection. The store's own transaction fails;
  nothing in the package is patched for it;
- a PostgreSQL schema per case (runner only) with server-side reads;
- a third-party external backend with its own failing `apply_capture_bundle`
  (the upstream test's FailingReferenceBackend, here a subclass of the bundled
  backend with the same class name).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from probe_449_lib import AGENT_ID

REFUSE_MESSAGE = "probe: capture receipt write refused"
TRIGGER = "probe_refuse_capture"


# ---------------------------------------------------------------- recovery steps
class StepRecorder:
    def __init__(self, runtime: Any, obs: Any) -> None:
        self.runtime = runtime
        self.obs = obs
        self.steps: list[dict[str, Any]] = []
        self.in_recovery = False
        self.label = ""
        self._root = None
        self._undo: list[tuple[Any, str, Any, bool]] = []
        self.capture_roots: dict[str, str] = {}

    def _add(self, step: str, root: Any, **extra: Any) -> None:
        self.steps.append({"seq": len(self.steps), "step": step, "root_turn_id": root,
                           "in_recovery": self.in_recovery, "label": self.label,
                           "thread": threading.current_thread().name, **extra})

    def _patch(self, obj: Any, name: str, make: Any, *, instance: bool) -> None:
        original = getattr(obj, name)
        had_own = instance and name in getattr(obj, "__dict__", {})
        setattr(obj, name, make(original))
        self._undo.append((obj, name, original, had_own or not instance))

    def install(self) -> "StepRecorder":
        import openminion.modules.memory.runtime.capture_recovery as recovery_module
        import openminion.services.runtime.cron.executor as executor_module

        rec = self

        def identity(original: Any) -> Any:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                root = kwargs.get("root_turn_id")
                rec._root = root
                rec._add("identity", root)
                return original(*args, **kwargs)
            return wrapper

        def source(original: Any) -> Any:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                root = kwargs.get("root_turn_id")
                try:
                    body = original(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    rec._add("source", root, raised=f"{type(exc).__name__}: {exc}")
                    raise
                rec._add("source", root)
                return body
            return wrapper

        def authorize(original: Any) -> Any:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                root = args[1] if len(args) > 1 else kwargs.get("root_turn_id")
                allowed = original(*args, **kwargs)
                rec._add("authorize", root, allowed=bool(allowed))
                return allowed
            return wrapper

        def capability(original: Any) -> Any:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                answer = original(*args, **kwargs)
                rec._add("capability", rec._root, answer=answer)
                return answer
            return wrapper

        def extract(original: Any) -> Any:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                root = kwargs.get("root_turn_id")
                try:
                    items = original(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    rec._add("extract", root, raised=f"{type(exc).__name__}: {exc}")
                    raise
                rec._add("extract", root, items=len(items or []))
                return items
            return wrapper

        def bundle(step: str) -> Any:
            def make(original: Any) -> Any:
                def wrapper(*args: Any, **kwargs: Any) -> Any:
                    root = kwargs.get("root_turn_id")
                    if root is None and args:
                        root = getattr(args[0], "root_turn_id", None)
                    try:
                        result = original(*args, **kwargs)
                    except Exception as exc:  # noqa: BLE001
                        rec._add(step, root, raised=f"{type(exc).__name__}: {exc}")
                        raise
                    rec._add(step, root)
                    return result
                return wrapper
            return make

        def result_write(original: Any) -> Any:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                capture_id = kwargs.get("capture_id")
                root = rec.capture_roots.get(str(capture_id), rec._root if rec.in_recovery else None)
                try:
                    out = original(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    rec._add("result", root, capture_id=capture_id, raised=f"{type(exc).__name__}: {exc}")
                    raise
                rec._add("result", root, capture_id=capture_id)
                return out
            return wrapper

        def recover(original: Any) -> Any:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                rec.in_recovery = True
                rec._add("recovery.start", None)
                try:
                    result = original(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    rec._add("recovery.raised", None, raised=f"{type(exc).__name__}: {exc}")
                    raise
                finally:
                    rec.in_recovery = False
                rec._add("recovery.end", None, result=[result.scanned, result.recovered, result.pending])
                return result
            return wrapper

        self._patch(recovery_module, "build_capture_identity", identity, instance=False)
        self._patch(recovery_module, "_capture_source_body", source, instance=False)
        self._patch(executor_module, "memory_capture_recovery_allowed", authorize, instance=False)
        memctl = self.obs.memctl
        if callable(getattr(memctl, "supports_capture_bundles", None)):
            self._patch(memctl, "supports_capture_bundles", capability, instance=True)
        self._patch(self.obs.service, "extract_memory_capture_candidates", extract, instance=True)
        self._patch(memctl, "apply_capture_bundle", bundle("bundle"), instance=True)
        # The memory service looks up its store's method on each call, so the store's write is recorded too.
        # (MemctlAdapter binds the service's method when it is built, so a service-level recorder would not see it.)
        service = getattr(self.obs.assembly, "service", None)
        store = getattr(service, "_store", None)
        if store is not None and callable(getattr(store, "apply_capture_bundle", None)):
            self._patch(store, "apply_capture_bundle", bundle("bundle.store"), instance=True)
        self._patch(self.runtime.sessions, "commit_capture_result_and_release_hold", result_write, instance=True)
        self._patch(self.obs.assembly, "recover_pending_captures", recover, instance=True)
        return self

    def uninstall(self) -> None:
        for obj, name, original, restore in reversed(self._undo):
            if restore:
                setattr(obj, name, original)
            else:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
        self._undo.clear()

    def mark(self) -> int:
        return len(self.steps)

    def since(self, mark: int) -> list[dict[str, Any]]:
        return self.steps[mark:]


def steps_by_root(steps: list[dict[str, Any]], labels: dict[str, str], *, recovery_only: bool = True) -> dict[str, list[str]]:
    """Per intent label, the recovery steps in order (with the answer or error where there is one)."""
    out: dict[str, list[str]] = {}
    for s in steps:
        if s["step"].startswith("recovery.") or (recovery_only and not s["in_recovery"]):
            continue
        who = labels.get(str(s.get("root_turn_id")), str(s.get("root_turn_id")))
        text = s["step"]
        if "allowed" in s:
            text += f"={s['allowed']}"
        if "answer" in s:
            text += f"={s['answer']}"
        if "raised" in s:
            text += f" raised {s['raised'].split(':')[0]}"
        out.setdefault(who, []).append(text)
    return out


def count_steps(steps: list[dict[str, Any]], name: str, *, recovery_only: bool = True) -> int:
    return sum(1 for s in steps if s["step"] == name and (s["in_recovery"] or not recovery_only))


# ---------------------------------------------------------------- readers
def parse(value: Any) -> dict[str, Any]:
    try:
        return json.loads(value) if isinstance(value, str) else dict(value or {})
    except (TypeError, ValueError):
        return {"unparsed": value}


def all_events(store: Any, event_type: str | None = None) -> list[Any]:
    out = []
    for session in store.list_sessions(limit=1000):
        for event in store.list_events(session_id=session.id, limit=10000):
            if event_type is None or event.event_type == event_type:
                out.append(event)
    return out


def intents(runtime: Any) -> list[dict[str, Any]]:
    results: dict[str, list[Any]] = {}
    for e in all_events(runtime.sessions, "memory.capture.result"):
        results.setdefault(str(e.payload.get("capture_id") or ""), []).append(e)
    out = []
    for event in all_events(runtime.sessions, "turn.outcome"):
        p = event.payload
        if str(p.get("agent_id") or "") != AGENT_ID:
            continue
        cid = str(p.get("capture_id") or "")
        res = results.get(cid, []) if cid else []
        out.append({"capture_id": cid, "capture_state": p.get("capture_state"), "has_result": bool(res),
                    "results": len(res),
                    "result_disposition": (res[0].payload or {}).get("disposition") if res else None,
                    "session": p.get("runtime_session_id"), "root_turn_id": p.get("root_turn_id"),
                    "canonical_event_id": event.canonical_event_id, "created_at": str(event.created_at),
                    "payload": dict(p)})
    return sorted(out, key=lambda i: (i["created_at"], str(i["session"]), i["capture_id"]))


def pending(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in items if i["capture_state"] == "pending" and not i["has_result"]]


def turn_diagnostics(runtime: Any) -> list[dict[str, Any]]:
    """Messages whose metadata carries a memory_capture_bundle_result (where the runtime keeps a turn's capture
    diagnostic), with the error code and a digest of the whole metadata."""
    import hashlib

    out = []
    for session in runtime.sessions.list_sessions(limit=1000):
        count = runtime.sessions.count_messages(session_id=session.id)
        for m in runtime.sessions.list_messages(session_id=session.id, limit=max(1, int(count))):
            md = dict(m.metadata or {})
            raw = md.get("memory_capture_bundle_result")
            if raw is None:
                continue
            res = parse(raw)
            out.append({"session": session.id, "role": m.role, "capture_id": res.get("capture_id"),
                        "disposition": res.get("disposition"), "error_code": res.get("error_code"),
                        "metadata_sha256": hashlib.sha256(json.dumps(md, sort_keys=True, default=str)
                                                          .encode()).hexdigest()})
    return out


# ---------------------------------------------------------------- real store write failure
def sqlite_trigger(db: Path, on: bool) -> str:
    with closing(sqlite3.connect(str(db), timeout=30)) as conn:
        if on:
            conn.execute(f"CREATE TRIGGER {TRIGGER} BEFORE INSERT ON memory_capture_bundle_receipts "
                         f"BEGIN SELECT RAISE(ABORT, '{REFUSE_MESSAGE}'); END")
        else:
            conn.execute(f"DROP TRIGGER IF EXISTS {TRIGGER}")
        conn.commit()
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()
    return f"triggers now: {[r[0] for r in rows]}"


def sqlite_count(db: Path, table: str, where: str = "", params: tuple = ()) -> Any:
    if not Path(db).is_file():
        return f"missing: {Path(db).name}"
    try:
        with closing(sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def sqlite_table_counts(db: Path) -> dict[str, Any] | str:
    if not Path(db).is_file():
        return f"missing: {Path(db).name}"
    with closing(sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1")]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


PG_BASE = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()


class PgSchema:
    """One PostgreSQL schema for a case's memory store; rows are read server-side (URL never printed)."""

    def __init__(self, label: str) -> None:
        import uuid
        from urllib.parse import quote_plus

        import sqlalchemy as sa

        self.sa = sa
        self.admin = sa.create_engine(PG_BASE, future=True)
        self.schema = f"probe449_{label}_{uuid.uuid4().hex[:10]}".lower()
        with self.admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{self.schema}"'))
        sep = "&" if "?" in PG_BASE else "?"
        self.url = f"{PG_BASE}{sep}options={quote_plus('-csearch_path=' + self.schema)}"

    def memory_config(self, **kwargs: Any) -> Any:
        import dataclasses

        from openminion.modules.memory.config import from_base_config

        memory = from_base_config(**kwargs)
        return dataclasses.replace(memory, store=dataclasses.replace(memory.store, backend="postgres",
                                                                      postgres_url=self.url))

    def rows(self, sql: str, params: dict[str, Any] | None = None) -> list[tuple] | str:
        try:
            with self.admin.connect() as conn:
                conn.execute(self.sa.text(f'SET search_path TO "{self.schema}"'))
                return [tuple(r) for r in conn.execute(self.sa.text(sql), params or {})]
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"

    def count(self, table: str, where: str = "", params: dict[str, Any] | None = None) -> Any:
        r = self.rows(f"SELECT COUNT(*) FROM {table} {where}", params)
        return r[0][0] if isinstance(r, list) else r

    def tables(self) -> list[str] | str:
        r = self.rows("SELECT table_name FROM information_schema.tables WHERE table_schema = :s ORDER BY 1",
                      {"s": self.schema})
        return [x[0] for x in r] if isinstance(r, list) else r

    def trigger(self, on: bool) -> str:
        sa = self.sa
        s = self.schema
        with self.admin.begin() as conn:
            if on:
                conn.execute(sa.text(
                    f'CREATE OR REPLACE FUNCTION "{s}".{TRIGGER}() RETURNS trigger LANGUAGE plpgsql AS '
                    f"$$ BEGIN RAISE EXCEPTION '{REFUSE_MESSAGE}'; END $$"))
                conn.execute(sa.text(
                    f'CREATE TRIGGER {TRIGGER} BEFORE INSERT ON "{s}".memory_capture_bundle_receipts '
                    f'FOR EACH ROW EXECUTE FUNCTION "{s}".{TRIGGER}()'))
            else:
                conn.execute(sa.text(f'DROP TRIGGER IF EXISTS {TRIGGER} ON "{s}".memory_capture_bundle_receipts'))
        r = self.rows("SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                      "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = :s AND NOT t.tgisinternal",
                      {"s": s})
        return f"triggers now: {[x[0] for x in r] if isinstance(r, list) else r}"

    def drop(self) -> None:
        with self.admin.begin() as conn:
            conn.execute(self.sa.text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
        self.admin.dispose()


def pg_server_info() -> dict[str, Any]:
    if not PG_BASE:
        return {"postgres": "not run: OPENMINION_TEST_POSTGRES_URL is not set"}
    import sqlalchemy as sa

    engine = sa.create_engine(PG_BASE, future=True)
    try:
        with engine.connect() as conn:
            return {"postgres_version": conn.execute(sa.text("SELECT version()")).scalar_one(),
                    "postgres_server_version_num": conn.execute(sa.text("SHOW server_version_num")).scalar_one()}
    finally:
        engine.dispose()


# ---------------------------------------------------------------- third-party backend
class ProbeBackendCaptureFailed(RuntimeError):
    """The error the probe backend's own `apply_capture_bundle` raises."""


def register_failing_backend(name: str) -> None:
    """A subclass of the bundled backend with its own `apply_capture_bundle`, which fails. It keeps the bundled
    class name, so the adapter name and class name are the reference backend's; only the method differs."""
    from sophiagraph.storage import SophiaGraphSqliteStore as Base

    from openminion.modules.memory.backends.external.registry import (
        ExternalBackendCapabilities,
        register_external_backend,
    )

    class SophiaGraphSqliteStore(Base):  # noqa: N801 - same name on purpose
        def apply_capture_bundle(self, bundle: Any) -> Any:
            raise ProbeBackendCaptureFailed("probe backend: capture write failed")

    def factory(*, config: Any, **_: Any) -> Any:
        return SophiaGraphSqliteStore(Path(str(config.options["db_path"])))

    register_external_backend(name, factory=factory, capabilities=ExternalBackendCapabilities())


def external_memory(root: Path, adapter: str) -> Any:
    config = {
        "store": {"backend": "sqlite", "sqlite_path": str(root / "memory.db")},
        "backend": {"provider": "external", "external_adapter": adapter,
                    "options": {"db_path": str(root / "external.sqlite3")}},
    }
    return lambda **_kwargs: config


def mock_memory() -> Any:
    config = {"store": {"backend": "mock"}}
    return lambda **_kwargs: config


# ---------------------------------------------------------------- cron
def create_job(cron: Any, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """The installed `task.consolidate_memory` handler, through the daemon's TaskManager (as in the reports)."""
    from openminion.modules.tool.runtime import RuntimeContext
    from openminion.modules.tool.runtime.policy import Policy
    from openminion.tools.task.plugin import _h_task_consolidate_memory

    workspace = cron.root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    policy = Policy(raw={"workspace_root": str(workspace), "context_metadata": {"agent_id": AGENT_ID},
                         "paths": {"read_allow": [str(workspace)], "write_allow": [str(workspace)], "deny": []},
                         "tools": {"allow_prefix": [""]}})
    ctx = RuntimeContext(policy=policy, workspace=workspace, run_root=cron.root / "tool-run", scope="WRITE_SAFE",
                         confirm=False, task_manager=cron.task_manager)
    return _h_task_consolidate_memory(dict(args or {"interval_hours": 12}), ctx)


def run_job(cron: Any, obs: Any, job_id: str, *, timeout_s: float = 180.0) -> dict[str, Any]:
    queued = cron.run_now(job_id)
    return wait_run(cron, obs, job_id, str(queued.get("run_id")), timeout_s=timeout_s)


def wait_run(cron: Any, obs: Any, job_id: str, run_id: str, *, timeout_s: float = 180.0) -> dict[str, Any]:
    done = cron.wait(job_id, run_id, timeout_s=timeout_s)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not any(
            e.get("event") in ("cron.run.finished", "cron.run.failed") and e.get("run_id") == run_id
            for e in obs.cron_events):
        time.sleep(0.05)
    return {"run_id": run_id, "state": done.get("state"), "error": done.get("error"),
            "summary": done.get("summary"), "probe_wait": done.get("probe_wait"),
            "result_summary": (parse(done.get("result")) or {}).get("summary") if done.get("result") else None}


def turn(agent_run: Any) -> dict[str, Any]:
    """An SDK turn; an exception it raises is recorded, as upstream's tests do for a failing backend."""
    try:
        res = agent_run()
    except Exception as exc:  # noqa: BLE001
        return {"raised": f"{type(exc).__name__}: {exc}"}
    md = dict(res.metadata or {})
    return {"run_state": res.run_state, "memory_capture_state": md.get("memory_capture_state"),
            "memory_capture_bundle_result": parse(md.get("memory_capture_bundle_result"))}
