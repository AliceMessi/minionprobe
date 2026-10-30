"""Helpers for probe_448.py (installed package only, no repo test fixtures).

On top of `probe_420_lib.py` (the harness of the #448 report and PR probes,
byte-identical, read-only here) this adds:

- build448: the same `APIRuntime.from_manager` runtime as probe_420_lib's
  `build_runtime` (one agent `openminion`, provider `openai` pointed at the
  scripted server on 127.0.0.1, single process, explicit roots), plus: runtime
  storage on PostgreSQL (`storage.backend: postgres`), an optional identity
  profile, and an optional registered memory config (the PostgreSQL memory
  store, or the bundled `reference-sqlite` external backend).
- ToolCallProvider: the scripted provider; when `write_tool_call` is set it
  answers the first non-forced request that offers the `memory_write` tool with
  one `memory_write` tool call (the PR test's model tool call).
- DecisionLog: a call-through recorder on the agent's SecurityPolicyEngine
  `evaluate` that keeps every `memory.write` decision and every `tool-runtime`
  decision, with the check's context (channel, target, session, run id) and
  actor. TurnLog: a call-through recorder on the agent service's
  `_execute_turn` that keeps the runner object and its per-turn capture flags.
- StepRecorder and the readers: from probe_449_ext.py of the 0.1.15
  verification (the #449 release probe), unchanged except the import: the
  steps of pending-capture recovery per intent and in order (identity check,
  authorization, capability query, source read, extraction, bundle writes,
  result write), the turn.outcome intents and their memory.capture.result
  events, the per-turn diagnostics in message metadata, and the cron helpers.
- PgCase: a new PostgreSQL database per case (runtime storage in it, the
  memory store in its own schema of it), with server-side reads. The URL is
  never printed.
- CLI helpers: the installed `openminion` console script as a subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlsplit, urlunsplit

from probe_420_lib import AGENT_ID, PROFILE_YAML, ScriptedProvider, _submit

WRITE_CONSTRAINT = "NEVER write memory records"
NEUTRAL_CONSTRAINT = "NEVER reveal secrets"
TOOL_ARGS = {"record_type": "fact", "key": "fact:indentation", "title": "Indentation",
             "content": "The user prefers tabs."}
KEY_ENV = "PROBE_420_PROVIDER_KEY"


# ---------------------------------------------------------------- runtime
def write_profile(root: Path, constraint: str) -> Path:
    path = root / "identity" / AGENT_ID / "profile.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PROFILE_YAML.format(constraint=constraint), encoding="utf-8")
    return path


def save448(root: Path, provider_url: str, *, constraint: str | None = None, storage_url: str | None = None) -> Path:
    from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config

    root.mkdir(parents=True, exist_ok=True)
    config = OpenMinionConfig()
    config.agents = {AGENT_ID: AgentProfileConfig(name=AGENT_ID, provider="openai", default_channel="console")}
    config.default_agent = AGENT_ID
    config.providers.openai.api_key_env = KEY_ENV
    config.providers.openai.base_url = provider_url
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / ".openminion" / "state.db")
    if storage_url:
        config.storage.backend = "postgres"
        config.storage.postgres_url = storage_url
    if constraint is not None:
        write_profile(root, constraint)
        config.identity.bundle_root = str(root / "identity")
        config.identity.db_path = str(root / ".openminion" / "identity.db")
    path = root / "agents.json"
    save_config(config, str(path))
    return path


def build448(root: Path, provider: ScriptedProvider, *, constraint: str | None = None, memory: Any = None,
             external: str | None = None, storage_url: str | None = None) -> Any:
    from openminion import APIRuntime
    from openminion.base.config import ConfigManager

    path = save448(root, provider.base_url, constraint=constraint, storage_url=storage_url)
    manager = ConfigManager.load(str(path), home_root=root, data_root=root / ".openminion")
    if external is not None:
        config = {"store": {"backend": "sqlite", "sqlite_path": str(root / "memory.db")},
                  "backend": {"provider": "external", "external_adapter": external,
                              "options": {"db_path": str(root / "external.sqlite3")}}}
        manager.register("memory", lambda **_kwargs: config)
    elif memory is not None:
        manager.register("memory", memory)
    return APIRuntime.from_manager(manager)


class ToolCallProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.write_tool_call = False
        self.tool_call_sent = False

    def respond(self, body: dict[str, Any], path: str) -> dict[str, Any]:
        names = [str((t.get("function") or {}).get("name", "")) for t in body.get("tools") or []]
        choice = body.get("tool_choice")
        forced = isinstance(choice, dict) and bool((choice.get("function") or {}).get("name"))
        if self.write_tool_call and not forced and "memory_write" in names and not self.tool_call_sent:
            with self.lock:
                self.tool_call_sent = True
                index = len(self.requests)
                self.requests.append({"index": index, "kind": "tool_call", "path": path, "model": body.get("model"),
                                      "mentions_capture_message": True, "t": time.monotonic()})
            call = _submit(TOOL_ARGS, f"call-memory-write-{index}")
            call["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "memory_write"
            return call
        return super().respond(body, path)


# ---------------------------------------------------------------- recorders
class DecisionLog:
    """Every memory.write decision and every tool-runtime decision of the agent's SecurityPolicyEngine."""

    def __init__(self, policy: Any) -> None:
        self.policy = policy
        self.rows: list[dict[str, Any]] = []
        self._inner = None

    def install(self) -> "DecisionLog":
        if self.policy is None:
            return self
        inner = self.policy.evaluate
        self._inner = inner
        log = self

        def evaluate(check: Any, *args: Any, **kwargs: Any) -> Any:
            decision = inner(check, *args, **kwargs)
            ctx = check.context
            if check.action.tool_name == "memory.write" or ctx.target == "tool-runtime":
                log.rows.append({"tool_name": check.action.tool_name, "channel": ctx.channel, "target": ctx.target,
                                 "session_id": ctx.session_id, "run_id": ctx.run_id,
                                 "actor_agent_id": getattr(check.actor, "agent_id", None),
                                 "actor_scopes": sorted(check.actor.scopes),
                                 "decision": decision.decision, "reason_code": decision.reason_code,
                                 "thread": threading.current_thread().name, "t": time.monotonic()})
            return decision

        self.policy.evaluate = evaluate
        return self

    def uninstall(self) -> None:
        if self.policy is not None and self._inner is not None:
            self.policy.evaluate = self._inner

    def mark(self) -> int:
        return len(self.rows)

    def since(self, mark: int, *, target: str | None = None, keep_scopes: bool = False) -> list[dict[str, Any]]:
        out = []
        for row in self.rows[mark:]:
            if target is not None and row["target"] != target:
                continue
            out.append({k: v for k, v in row.items() if k not in ("t", "thread") and (keep_scopes or k != "actor_scopes")})
        return out


class TurnLog:
    """The runner each turn executes on, and its per-turn capture flags (call-through on `_execute_turn`)."""

    FLAGS = ("_capture_authorized_for_turn", "_capture_excluded_for_turn", "_terminal_capture_enabled_for_turn",
             "_memory_write_allowed_for_turn")

    def __init__(self, service: Any) -> None:
        self.service = service
        self.rows: list[dict[str, Any]] = []
        self.runner_ids: dict[int, int] = {}

    def install(self) -> "TurnLog":
        original = self.service._execute_turn
        log = self

        def wrapper(**kwargs: Any) -> Any:
            runner = kwargs.get("runner")
            rid = log.runner_ids.setdefault(id(runner), len(log.runner_ids) + 1)
            message = kwargs.get("message")
            log.rows.append({"runner": rid, "session_id": kwargs.get("session_id"),
                             "channel": getattr(message, "channel", None),
                             **{f.lstrip("_"): getattr(runner, f, "absent") for f in TurnLog.FLAGS}})
            return original(**kwargs)

        self.service._execute_turn = wrapper
        return self

    def uninstall(self) -> None:
        try:
            del self.service._execute_turn
        except AttributeError:
            pass


# ---------------------------------------------------------------- recovery steps (from probe_449_ext.py)
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


# ---------------------------------------------------------------- readers (from probe_449_ext.py)
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
                    "result_output_ids": list((res[0].payload or {}).get("output_ids") or []) if res else None,
                    "session": p.get("runtime_session_id"), "root_turn_id": p.get("root_turn_id"),
                    "canonical_event_id": event.canonical_event_id, "created_at": str(event.created_at),
                    "payload": dict(p)})
    return sorted(out, key=lambda i: (i["created_at"], str(i["session"]), i["capture_id"]))


def event_types(runtime: Any, session_id: str) -> list[str]:
    try:
        return [e.event_type for e in runtime.sessions.list_events(session_id=session_id, limit=10000)]
    except Exception as exc:  # noqa: BLE001
        return [f"{type(exc).__name__}: {exc}"]


# ---------------------------------------------------------------- cron (from probe_449_ext.py)
def create_job(cron: Any, args: dict[str, Any] | None = None) -> dict[str, Any]:
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
    run_id = str(queued.get("run_id"))
    done = cron.wait(job_id, run_id, timeout_s=timeout_s)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not any(
            e.get("event") in ("cron.run.finished", "cron.run.failed") and e.get("run_id") == run_id
            for e in obs.cron_events):
        time.sleep(0.05)
    return {"run_id": run_id, "state": done.get("state"), "error": done.get("error"),
            "summary": done.get("summary"), "probe_wait": done.get("probe_wait")}


# ---------------------------------------------------------------- PostgreSQL
def pg_base() -> str:
    return (os.environ.get("PGURL") or os.environ.get("OPENMINION_TEST_POSTGRES_URL") or "").strip()


def _psycopg(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme in ("postgresql", "postgres"):
        parts = parts._replace(scheme="postgresql+psycopg")
    return urlunsplit(parts)


class PgCase:
    """A new database for one case: runtime storage in it, the memory store in its own schema of it."""

    def __init__(self, label: str) -> None:
        import uuid

        import sqlalchemy as sa

        self.sa = sa
        base = _psycopg(pg_base())
        self.dbname = f"probe448_{label}_{uuid.uuid4().hex[:8]}".lower()
        admin = sa.create_engine(base, future=True, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{self.dbname}"'))
        admin.dispose()
        self._admin_base = base
        self.url = urlunsplit(urlsplit(base)._replace(path=f"/{self.dbname}"))
        self.engine = sa.create_engine(self.url, future=True)
        self.schema = f"memory_{label}".lower()
        with self.engine.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{self.schema}"'))
        sep = "&" if "?" in self.url else "?"
        self.memory_url = f"{self.url}{sep}options={quote_plus('-csearch_path=' + self.schema)}"

    def memory_config(self, **kwargs: Any) -> Any:
        import dataclasses

        from openminion.modules.memory.config import from_base_config

        memory = from_base_config(**kwargs)
        return dataclasses.replace(memory, store=dataclasses.replace(memory.store, backend="postgres",
                                                                      postgres_url=self.memory_url))

    def rows(self, sql: str, params: dict[str, Any] | None = None) -> list[tuple] | str:
        try:
            with self.engine.connect() as conn:
                return [tuple(r) for r in conn.execute(self.sa.text(sql), params or {})]
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"

    def table_counts(self) -> dict[str, Any] | str:
        names = self.rows("SELECT table_schema, table_name FROM information_schema.tables "
                          "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2")
        if not isinstance(names, list):
            return names
        out: dict[str, Any] = {}
        for schema, table in names:
            r = self.rows(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
            out[f"{schema}.{table}"] = r[0][0] if isinstance(r, list) else r
        return out

    def event_counts(self, event_types: tuple[str, ...] = ("turn.outcome", "memory.capture.result")) -> dict[str, Any]:
        """Server-side counts of the runtime's session events by type, in every table with an event_type column."""
        tables = self.rows("SELECT table_schema, table_name FROM information_schema.columns WHERE column_name = "
                           "'event_type' AND table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2")
        if not isinstance(tables, list):
            return {"error": tables}
        out: dict[str, Any] = {}
        for schema, table in tables:
            for et in event_types:
                r = self.rows(f'SELECT COUNT(*) FROM "{schema}"."{table}" WHERE event_type = :t', {"t": et})
                out[f"{schema}.{table}:{et}"] = r[0][0] if isinstance(r, list) else r
        return out

    def connections(self) -> Any:
        r = self.rows("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                      "AND pid <> pg_backend_pid()")
        return r[0][0] if isinstance(r, list) else r

    def drop(self) -> None:
        self.engine.dispose()
        admin = self.sa.create_engine(self._admin_base, future=True, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(self.sa.text(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)'))
        admin.dispose()


def pg_server_info() -> dict[str, Any]:
    if not pg_base():
        return {"postgres": "not run: no PostgreSQL URL"}
    import sqlalchemy as sa

    engine = sa.create_engine(_psycopg(pg_base()), future=True)
    try:
        with engine.connect() as conn:
            return {"postgres_version": conn.execute(sa.text("SELECT version()")).scalar_one()}
    finally:
        engine.dispose()


# ---------------------------------------------------------------- CLI
STRIP_ENV = ("OPENMINION_HOME", "OPENMINION_DATA_ROOT", "OPENMINION_GENERATED_ROOT", "OPENMINION_CONFIG_PATH",
             "OPENMINION_STORAGE_BACKEND", "OPENMINION_STORAGE_POSTGRES_URL", "OPENMINION_DISABLE_SECURITY_POLICY")


def openminion_exe() -> str:
    name = "openminion.exe" if os.name == "nt" else "openminion"
    return str(Path(sys.executable).with_name(name))


def run_cli(root: Path, cfg: Path, argv: list[str], *, timeout: float = 300) -> dict[str, Any]:
    full = ["--home-root", str(root), "--data-root", str(root / ".openminion"), "--config", str(cfg), *argv]
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env["PYTHONIOENCODING"] = "utf-8"
    env[KEY_ENV] = "local-dummy-key"
    start = time.monotonic()
    proc = subprocess.run([openminion_exe(), *full], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=str(root), timeout=timeout, check=False)
    return {"argv": ["openminion", *full], "exit_code": proc.returncode, "stdout": proc.stdout,
            "stderr_tail": [ln for ln in proc.stderr.strip().splitlines() if ln.strip()][-6:],
            "seconds": round(time.monotonic() - start, 2)}


def first_json(text: str) -> Any:
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                value, _end = decoder.raw_decode(text[i:])
                return value
            except json.JSONDecodeError:
                continue
    return None
