"""Helpers for probe_496.py (installed package only, no repo test fixtures).

Built on the #448 probe's harness `probe_420_lib.py` (unchanged): a real
`APIRuntime` for one agent, `openminion`, whose `openai` provider is a scripted
OpenAI-compatible server on 127.0.0.1. This module adds what the #496 report's
probe (`probe_turn_memory_writes.py`) added, and a few more call-through
recorders. Every recorder calls the original and never changes a result:

- TurnProvider: the scripted server plus one `memory_write` tool call when a
  turn forces `memory.write`, and the consolidation turn's act-loop entry.
- Recorder: the brain memory API's `write_record`, `stage_candidate` and
  `apply_outcome_feedback`, and every `memory.write` decision of the agent's
  security policy (target, decision, reason code).
- DecisionMemoryRecorder: `entry.write_decision_memory` (the call the turn
  dispatch makes, with the working state's decision fields at that moment and
  the runner's per-turn memory.write answer) and the `brain.decision_memory.*`
  events it emits.
- RunnerTurnRecorder: the agent service's `_execute_turn`, to see which runner
  object each turn uses and the per-turn flags the bridge set on it.
- MemDB: reads the memory store: the SQLite memory database (read-only
  connection) or one PostgreSQL schema (server-side, through SQLAlchemy). It
  lists `decision` records, `tool_outcome` candidates and capture rows, and
  searches every table of the store for given texts.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import probe_420_lib as lib
from probe_420_lib import AGENT_ID, CAPTURE_ITEM, CAPTURE_MESSAGE, ScriptedProvider

WRITE_CONSTRAINT = "NEVER write memory records"
NEUTRAL_CONSTRAINT = "NEVER reveal secrets"
QUESTION = "Which indentation style do I prefer?"
CONSOLIDATION_MESSAGE = "memory_consolidation trailer"
HELLO = "Say hello."
TOOL_ARGS = {"record_type": "fact", "key": "fact:indentation", "title": "Indentation",
             "content": "The user prefers tabs."}
CANONICAL_ARGS = json.dumps(TOOL_ARGS, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
# Texts that only the refused memory.write call carries (the capture's own item says
# "The user prefers tabs over spaces for indentation." under "user_preference:indentation").
REFUSED_NEEDLES = ("The user prefers tabs.", "fact:indentation")
TOOL_OUTCOME_TITLE = "tool_outcome:memory.write:policy_denied"
PG_BASE = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()


# ---------------------------------------------------------------- provider
def _last_user(body: dict[str, Any]) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            return str(content or "")
    return ""


class TurnProvider(ScriptedProvider):
    """The #496 report probe's scripted server (without its promote reply)."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.write_tool_call = False
        self.consolidation_entry_done = False
        super().__init__()

    def respond(self, body: dict[str, Any], path: str) -> dict[str, Any]:
        txt = lib._all_text(body)
        last_user = _last_user(body)
        names = [str((t.get("function") or {}).get("name", "")) for t in body.get("tools") or []
                 if isinstance(t, dict)]
        choice = body.get("tool_choice")
        forced = str((choice.get("function") or {}).get("name", "")) if isinstance(choice, dict) else ""
        with self.lock:
            if forced == "submit_output":
                if "purpose: reflect" in txt:
                    kind = "extraction"
                elif "purpose: judge" in txt or "[JUDGE CONTEXT]" in txt:
                    kind = "judge"
                elif "freshness-routing classifier" in txt:
                    kind = "freshness"
                else:
                    kind = "other_structured"
            elif self.write_tool_call and "memory_write" in names:
                kind = "tool_call"
                self.write_tool_call = False
            elif (CONSOLIDATION_MESSAGE in last_user and "decompose" in names
                  and not self.consolidation_entry_done):
                kind = "consolidation_entry"
                self.consolidation_entry_done = True
            elif CONSOLIDATION_MESSAGE in last_user:
                kind = "consolidation_other"
            elif "purpose: decide" in txt:
                kind = "decide"
            else:
                kind = "other_text"
            index = len(self.requests)
            self.requests.append({"index": index, "kind": kind, "path": path, "model": body.get("model"),
                                  "tools": sorted(n for n in names if n), "forced": forced or None,
                                  "t": time.monotonic()})
            self.texts.append(txt)
        if kind == "extraction":
            items = [dict(CAPTURE_ITEM)] if CAPTURE_MESSAGE in txt else []
            return lib._submit({"items": items}, f"call-extract-{index}")
        if kind == "judge":
            return lib._submit({"final_answer": "Noted.", "satisfied": True, "next_action": "close"},
                               f"call-judge-{index}")
        if kind == "freshness":
            return lib._submit({"intent": "statement", "domain": "general", "time_sensitive": False,
                                "needs_live_data": False, "needs_sources": False, "needs_exact_date": False,
                                "answer_mode": "direct", "reason": "No live data needed.", "confidence": 1},
                               f"call-freshness-{index}")
        if kind == "other_structured":
            return lib._submit({}, f"call-other-{index}")
        if kind == "tool_call":
            call = {"id": f"call-memory-write-{index}", "type": "function",
                    "function": {"name": "memory_write", "arguments": json.dumps(TOOL_ARGS)}}
            return lib._completion({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")
        if kind == "consolidation_entry":
            call = {"id": f"call-decompose-{index}", "type": "function",
                    "function": {"name": "decompose", "arguments": json.dumps({"subtasks": []})}}
            return lib._completion({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")
        if CAPTURE_MESSAGE in last_user:
            return lib._text("Noted: tabs over spaces.")
        if QUESTION in last_user:
            return lib._text("You prefer tabs.")
        if HELLO in last_user:
            return lib._text("Hello.")
        return lib._text("Consolidation reviewed; nothing to change.")

    def kinds(self, start: int, end: int) -> list[str]:
        with self.lock:
            return [r["kind"] for r in self.requests[start:end]]


# ---------------------------------------------------------------- recorders
def _rel(filename: str) -> str:
    return filename.replace("\\", "/").rsplit("/openminion/", 1)[-1]


def _callers(depth: int = 4) -> list[str]:
    frames = [f for f in traceback.extract_stack()[:-2] if "/openminion/" in f.filename.replace("\\", "/")]
    return [f"{_rel(f.filename)}:{f.lineno} {f.name}" for f in reversed(frames[-depth:])]


class Recorder:
    """Call-through recorders: the brain memory API's writes and the policy's memory.write decisions."""

    KEEP = {
        "write_record": ("scope", "record_type", "key", "title", "tags"),
        "stage_candidate": ("scope", "record_type", "title", "tags"),
        "apply_outcome_feedback": ("record_ids", "outcome", "command_id", "feedback_delta"),
    }

    def __init__(self, memory_api: Any, policy: Any) -> None:
        self.writes: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self._undo: list[tuple[Any, str, Any]] = []
        for name in self.KEEP:
            if callable(getattr(memory_api, name, None)):
                self._wrap(memory_api, name)
        if policy is not None:
            original = policy.evaluate

            def evaluate(check: Any, *args: Any, **kwargs: Any) -> Any:
                decision = original(check, *args, **kwargs)
                if getattr(check.action, "tool_name", None) == "memory.write":
                    ctx = check.context
                    self.decisions.append({"channel": ctx.channel, "target": ctx.target,
                                           "session_id": ctx.session_id,
                                           "decision": decision.decision, "reason_code": decision.reason_code,
                                           "thread": threading.current_thread().name, "t": time.monotonic()})
                return decision

            policy.evaluate = evaluate
            self._undo.append((policy, "evaluate", original))

    def _wrap(self, api: Any, name: str) -> None:
        original = getattr(api, name)
        keep = self.KEEP[name]

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            row: dict[str, Any] = {"method": name, "callers": _callers(), "t": time.monotonic(),
                                   **{k: kwargs.get(k) for k in keep if k in kwargs}}
            try:
                result = original(*args, **kwargs)
                row["result"] = result if isinstance(result, (str, int, float)) else repr(result)[:200]
                return result
            except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
                row["raised"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self.writes.append(row)

        setattr(api, name, wrapper)
        self._undo.append((api, name, None))

    def uninstall(self) -> None:
        for obj, name, original in reversed(self._undo):
            try:
                if original is None:
                    delattr(obj, name)
                else:
                    setattr(obj, name, original)
            except AttributeError:
                pass
        self._undo.clear()


class DecisionMemoryRecorder:
    """`entry.write_decision_memory` (as the turn dispatch calls it) and the decision-memory events."""

    def __init__(self) -> None:
        import openminion.modules.brain.execution.entry as entry
        import openminion.modules.brain.execution.memory.base as base

        self.calls: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self._undo = [(entry, "write_decision_memory", entry.write_decision_memory),
                      (base, "_emit_memory_event", base._emit_memory_event)]
        write, emit = self._undo[0][2], self._undo[1][2]

        def write_rec(runner: Any, *args: Any, **kwargs: Any) -> Any:
            state, decision = kwargs.get("state"), kwargs.get("decision")
            row: dict[str, Any] = {
                "t": time.monotonic(),
                "session_id": getattr(state, "session_id", None),
                "trace_id": getattr(state, "trace_id", None),
                "runner_memory_write_allowed_for_turn": getattr(runner, "_memory_write_allowed_for_turn", None),
                "decision_route": str(getattr(decision, "route", "") or ""),
                "decision_reason_code": str(getattr(decision, "reason_code", "") or ""),
                "state_decision_reason_code": getattr(state, "decision_reason_code", None),
                "state_decision_rationale": str(getattr(state, "decision_rationale", "") or "")[:160],
                "state_decision_success_criteria_keys": sorted(
                    (getattr(state, "decision_success_criteria", None) or {}).keys()),
                "state_decision_memory_refs_before": list(getattr(state, "decision_memory_refs", []) or []),
            }
            try:
                result = write(runner, *args, **kwargs)
                row["returned"] = list(result or [])
                return result
            except Exception as exc:  # noqa: BLE001
                row["raised"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self.calls.append(row)

        def emit_rec(logger: Any, state: Any, event: str, payload: dict[str, Any], **kwargs: Any) -> Any:
            if str(event).startswith("brain.decision_memory."):
                self.events.append({"event": event, "reason": (payload or {}).get("reason"),
                                    "record_id": (payload or {}).get("record_id"),
                                    "session_id": getattr(state, "session_id", None), "t": time.monotonic()})
            return emit(logger, state, event, payload, **kwargs)

        entry.write_decision_memory = write_rec
        base._emit_memory_event = emit_rec

    def uninstall(self) -> None:
        for module, name, original in self._undo:
            setattr(module, name, original)


class RunnerTurnRecorder:
    """The agent service's `_execute_turn`: the runner each turn runs on and its per-turn flags."""

    FLAGS = ("_memory_write_allowed_for_turn", "_capture_authorized_for_turn",
             "_capture_excluded_for_turn", "_terminal_capture_enabled_for_turn")

    def __init__(self, service: Any) -> None:
        self.service = service
        self.rows: list[dict[str, Any]] = []
        original = service._execute_turn

        def wrapper(**kwargs: Any) -> Any:
            runner = kwargs.get("runner")
            message = kwargs.get("message")
            self.rows.append({"runner_id": id(runner), "session_id": kwargs.get("session_id"),
                              "body": str(getattr(message, "body", "") or "")[:60],
                              **{f.lstrip("_"): getattr(runner, f, None) for f in self.FLAGS},
                              "t": time.monotonic()})
            return original(**kwargs)

        service._execute_turn = wrapper

    def uninstall(self) -> None:
        try:
            del self.service._execute_turn
        except AttributeError:
            pass


# ---------------------------------------------------------------- memory store
def _parse(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return value


class PgSchema:
    """One PostgreSQL schema for a case's memory store; rows are read server-side (URL never printed)."""

    def __init__(self, label: str) -> None:
        import uuid
        from urllib.parse import quote_plus

        import sqlalchemy as sa

        self.sa = sa
        self.admin = sa.create_engine(PG_BASE, future=True)
        self.schema = f"probe496_{label}_{uuid.uuid4().hex[:10]}".lower()
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

    def drop(self) -> None:
        try:
            with self.admin.begin() as conn:
                conn.execute(self.sa.text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
        finally:
            self.admin.dispose()


class MemDB:
    """The memory store's rows: a SQLite file (read-only connection) or a PostgreSQL schema."""

    def __init__(self, sqlite_path: Path | None = None, pg: PgSchema | None = None) -> None:
        self.sqlite_path = sqlite_path
        self.pg = pg
        self.backend = "postgres" if pg is not None else "sqlite"

    def rows(self, sql: str) -> list[tuple] | str:
        if self.pg is not None:
            sa = self.pg.sa
            try:
                with self.pg.admin.connect() as conn:
                    conn.execute(sa.text(f'SET search_path TO "{self.pg.schema}"'))
                    return [tuple(r) for r in conn.execute(sa.text(sql))]
            except Exception as exc:  # noqa: BLE001
                return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
        if self.sqlite_path is None:
            return "no memory database"
        return lib.sqlite_rows(self.sqlite_path, sql)

    def tables(self) -> list[str] | str:
        if self.pg is not None:
            r = self.rows("SELECT table_name FROM information_schema.tables WHERE table_schema = "
                          f"'{self.pg.schema}' AND table_type = 'BASE TABLE' ORDER BY 1")
        else:
            r = self.rows("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1")
        return [x[0] for x in r] if isinstance(r, list) else r

    def records(self) -> list[dict[str, Any]]:
        cols = ("id", "scope", "type", "key", "title", "content_json", "source", "created_at")
        r = self.rows(f"SELECT {', '.join(cols)} FROM memory_records ORDER BY created_at, id")
        if isinstance(r, str):
            return [{"error": r}]
        return [{c: (_parse(v) if c.endswith("_json") else v) for c, v in zip(cols, row)} for row in r]

    def candidates(self) -> list[dict[str, Any]]:
        cols = ("candidate_id", "session_id", "proposed_scope", "type", "key", "title", "content_json",
                "meta_json", "status", "source", "created_at")
        r = self.rows(f"SELECT {', '.join(cols)} FROM memory_candidates ORDER BY created_at, candidate_id")
        if isinstance(r, str):
            return [{"error": r}]
        return [{c: (_parse(v) if c.endswith("_json") else v) for c, v in zip(cols, row)} for row in r]

    def receipts(self, capture_id: str) -> Any:
        safe = capture_id.replace("'", "''")
        r = self.rows(f"SELECT COUNT(*) FROM memory_capture_bundle_receipts WHERE capture_id = '{safe}'")
        return r[0][0] if isinstance(r, list) else r

    def capture_rows(self, capture_id: str) -> dict[str, Any]:
        """Candidates and records whose meta carries the capture id."""
        out: dict[str, Any] = {}
        for table in ("memory_candidates", "memory_records"):
            r = self.rows(f"SELECT meta_json FROM {table}")
            if isinstance(r, str):
                out[table] = r
                continue
            out[table] = sum(1 for (meta,) in r if (_parse(meta) or {}).get("capture_id") == capture_id)
        return out

    def search(self, needles: tuple[str, ...]) -> dict[str, dict[str, int]]:
        """Rows of every table of the store whose text holds each needle."""
        hits: dict[str, dict[str, int]] = {n: {} for n in needles}
        tables = self.tables()
        if isinstance(tables, str):
            return {n: {"error": tables} for n in needles}  # type: ignore[dict-item]
        for table in tables:
            if self.pg is not None:
                r = self.rows(f'SELECT t::text FROM "{self.pg.schema}"."{table}" AS t')
            else:
                r = self.rows(f'SELECT * FROM "{table}"')
            if isinstance(r, str):
                for n in needles:
                    hits[n][table] = -1
                continue
            texts = [" | ".join(str(v) for v in row) for row in r]
            for n in needles:
                count = sum(1 for t in texts if n in t)
                if count:
                    hits[n][table] = count
        return hits


def other_sqlite_hits(root: Path, skip: Path | None, needles: tuple[str, ...]) -> dict[str, dict[str, int]]:
    """For the record only: the same texts in the case's other SQLite files (session transcript etc.)."""
    import sqlite3
    from contextlib import closing

    out: dict[str, dict[str, int]] = {}
    for path in sorted(root.rglob("*.db")) + sorted(root.rglob("*.sqlite3")):
        if skip is not None and path.resolve() == skip.resolve():
            continue
        try:
            with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
                found: dict[str, int] = {}
                for table in tables:
                    try:
                        rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
                    except sqlite3.DatabaseError:
                        continue
                    texts = [" | ".join(str(v) for v in row) for row in rows]
                    for n in needles:
                        count = sum(1 for t in texts if n in t)
                        if count:
                            found[f"{table}: {n}"] = found.get(f"{table}: {n}", 0) + count
        except sqlite3.DatabaseError as exc:
            found = {"error": str(exc)}  # type: ignore[dict-item]
        if found:
            out[path.relative_to(root).as_posix()] = found
    return out


def memory_db_path(obs: Any) -> Path | None:
    store = obs.assembly.service._store
    inner = getattr(store, "_store", None)
    return Path(str(inner.db_path)) if inner is not None and getattr(inner, "db_path", None) else None


def store_chain(obs: Any) -> list[str]:
    out, obj = [], obs.assembly.service._store
    for _ in range(4):
        if obj is None:
            break
        out.append(type(obj).__name__)
        obj = getattr(obj, "_store", None)
    return out


def event_type_counts(sessions: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in lib.session_events(sessions):
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    return counts


def counts_diff(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {k: after[k] - before.get(k, 0) for k in sorted(after) if after[k] != before.get(k, 0)}


def working_state(runner: Any, session_id: str | None) -> dict[str, Any] | None:
    """The persisted working state's decision fields for a brain session (None if there is none)."""
    if not session_id:
        return None
    try:
        state = runner.session_api.get_latest_working_state(session_id, agent_id=AGENT_ID) or {}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    inline = state.get("state_inline") if isinstance(state.get("state_inline"), dict) else state
    if not isinstance(inline, dict) or not inline:
        return None
    keep = ("decision_reason_code", "decision_rationale", "decision_memory_refs", "decision_capability_category",
            "decision_sub_intents", "pending_policy_approval_id", "working_route_source")
    out = {k: inline.get(k) for k in keep if k in inline}
    if isinstance(out.get("decision_rationale"), str):
        out["decision_rationale"] = out["decision_rationale"][:160]
    out["route"] = inline.get("route") or inline.get("decision_route") or inline.get("last_route")
    return out


def short_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:300] if str(exc) else ''}"


def strip_t(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if k != "t"} for r in rows]


def needle_hits_in_requests(provider: TurnProvider, start: int, end: int, needles: tuple[str, ...]) -> list[dict]:
    hits = []
    for index in range(start, min(end, len(provider.texts))):
        for n in needles:
            if n in provider.texts[index]:
                m = re.search(r"[^\n]{0,60}" + re.escape(n) + r"[^\n]{0,60}", provider.texts[index])
                hits.append({"request": index, "kind": provider.requests[index]["kind"], "needle": n,
                             "line": m.group(0) if m else None})
    return hits
