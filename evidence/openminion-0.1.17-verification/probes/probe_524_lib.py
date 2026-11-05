"""Helpers for probe_524.py (installed package only, no repo test fixtures).

Built on the #448/#496 probe harness `probe_420_lib.py` and `probe_496_lib.py`
(byte copies of the 0.1.16 round's files):

- build_runtime_ext: `probe_420_lib.build_runtime` (a real `APIRuntime` for one
  agent, `openminion`, whose `openai` provider is a scripted OpenAI-compatible
  server on 127.0.0.1), with two optional additions written into the same
  config file before it is loaded: exact-tool `action_policy` rules (mode
  `block`), and further agents on the same provider.
- ArmedProvider: the #448 scripted server; each `arm(tool, args)` makes the
  model call that tool once with those arguments (the next request that offers
  the tool and is not a forced structured call). Text replies are fixed.
- HandlerRecorder: replaces a tool's handler with a call-through recorder. Mode
  `pass` calls the original handler (memory.write), `stub` returns a fixed
  result without calling the original (weather: no network), `raise` raises
  RuntimeError after recording.
- Readers: the memory store (`probe_496_lib.MemDB`: SQLite read-only, or one
  PostgreSQL schema server-side), the policy store (`policy_decisions`,
  `policy_pending_confirmations`, `policy_grants`), the brain session events
  and the memory audit events, all read-only.
- PgMemorySchema: `probe_496_lib.PgSchema` (a new PostgreSQL schema per case, the
  memory store's URL carrying `search_path` = that schema), whose readers follow
  the schema that actually holds the memory store's tables: since #446 (0.1.17)
  the memory module creates its own schema `<schema>__memory` under the
  configured one; 0.1.16 uses the configured schema itself. Both are dropped.
- Standalone: a `BrainRunner` built from the installed package's own local
  adapters (no agent service, no runtime), as the upstream runner tests build
  it, with a tool adapter whose extra tools return a fixed status.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import probe_420_lib as lib
from probe_420_lib import AGENT_ID, CAPTURE_MESSAGE, PROFILE_YAML, ScriptedProvider

MEMORY_ARGS = {"record_type": "fact", "key": "fact:editor_font", "title": "Editor font",
               "content": "The user writes code in Iosevka 14."}
WEATHER_ARGS = {"location": "Probe524ville"}
# Texts that only the call's arguments carry.
MEMORY_NEEDLES = ("fact:editor_font", "Iosevka 14")
WEATHER_NEEDLES = ("Probe524ville",)
MESSAGE = "Remember that I write code in Iosevka 14."
WEATHER_MESSAGE = "What is the weather in Probe524ville?"
TEXT_REPLY = "Done."


# ---------------------------------------------------------------- runtime
def build_runtime_ext(root: Path, provider: ScriptedProvider, *, identity_constraint: str | None = None,
                      memory_config: Any = None, block_tools: tuple[str, ...] = (),
                      extra_agents: tuple[str, ...] = ()) -> Any:
    """probe_420_lib.build_runtime plus `action_policy` block rules and further agents."""
    from openminion import APIRuntime
    from openminion.base.config import AgentProfileConfig, ConfigManager, OpenMinionConfig, save_config
    from openminion.base.config.core import ActionPolicyMatchConfig, ActionPolicyRuleConfig

    root.mkdir(parents=True, exist_ok=True)
    config = OpenMinionConfig()
    config.agents = {AGENT_ID: AgentProfileConfig(name=AGENT_ID, provider="openai", default_channel="console")}
    for name in extra_agents:
        config.agents[name] = AgentProfileConfig(name=name, provider="openai", default_channel="console")
    config.default_agent = AGENT_ID
    config.providers.openai.api_key_env = "PROBE_420_PROVIDER_KEY"
    config.providers.openai.base_url = provider.base_url
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / ".openminion" / "state.db")
    for tool in block_tools:
        config.action_policy.rules.append(
            ActionPolicyRuleConfig(match=ActionPolicyMatchConfig(tool_name=tool), mode="block"))
    if identity_constraint is not None:
        identity_root = root / "identity"
        (identity_root / AGENT_ID).mkdir(parents=True, exist_ok=True)
        (identity_root / AGENT_ID / "profile.yaml").write_text(
            PROFILE_YAML.format(constraint=identity_constraint), encoding="utf-8")
        config.identity.bundle_root = str(identity_root)
        config.identity.db_path = str(root / ".openminion" / "identity.db")
    save_config(config, str(root / "agents.json"))
    manager = ConfigManager.load(str(root / "agents.json"), home_root=root, data_root=root / ".openminion")
    if memory_config is not None:
        manager.register("memory", memory_config)
    return APIRuntime.from_manager(manager)


# ---------------------------------------------------------------- provider
def _offered(body: dict[str, Any]) -> list[str]:
    return [str((t.get("function") or {}).get("name", "")) for t in body.get("tools") or [] if isinstance(t, dict)]


class ArmedProvider(ScriptedProvider):
    """The #448 scripted server; each `arm` makes the model call one tool once."""

    def __init__(self) -> None:
        self.armed: list[tuple[str, dict[str, Any]]] = []
        self.tool_calls: list[dict[str, Any]] = []
        super().__init__()

    def arm(self, model_tool: str, args: dict[str, Any]) -> None:
        with self.lock:
            self.armed.append((model_tool, dict(args)))

    def respond(self, body: dict[str, Any], path: str) -> dict[str, Any]:
        choice = body.get("tool_choice")
        forced = str((choice.get("function") or {}).get("name", "")) if isinstance(choice, dict) else ""
        names = _offered(body)
        call = None
        with self.lock:
            if forced != "submit_output" and self.armed and self.armed[0][0] in names:
                model_tool, args = self.armed.pop(0)
                index = len(self.requests)
                call = {"id": f"call-{model_tool}-{index}", "type": "function",
                        "function": {"name": model_tool, "arguments": json.dumps(args)}}
                self.requests.append({"index": index, "kind": f"tool_call:{model_tool}", "path": path,
                                      "model": body.get("model"), "t": time.monotonic()})
                self.tool_calls.append({"tool": model_tool, "args": args})
        if call is not None:
            return lib._completion({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")
        reply = super().respond(body, path)
        message = reply["choices"][0]["message"]
        if message.get("content") == "Consolidation reviewed; nothing to change.":
            message["content"] = TEXT_REPLY  # the helpers' default text reply, renamed
        return reply

    def kinds(self, start: int) -> list[str]:
        with self.lock:
            return [r["kind"] for r in self.requests[start:]]


# ---------------------------------------------------------------- handler recorder
class HandlerRecorder:
    def __init__(self, runtime: Any, tool: str, mode: str, result: Any = None) -> None:
        self.spec = runtime.tools.get(tool)
        self.original = self.spec.handler
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        recorder = self

        def handler(args: Any, context: Any) -> Any:
            with recorder.lock:
                recorder.calls.append(dict(args or {}))
            if mode == "raise":
                raise RuntimeError("probe: tool backend unavailable")
            if mode == "stub":
                return json.loads(json.dumps(result))
            return recorder.original(args, context)

        self.spec.handler = handler

    def restore(self) -> None:
        self.spec.handler = self.original


def model_args(calls: list[dict[str, Any]], keys: dict[str, Any]) -> list[dict[str, Any]]:
    """The model's keys of each recorded call (a validated call carries its defaults too)."""
    return [{k: c.get(k) for k in keys} for c in calls]


# ---------------------------------------------------------------- readers
def ro_rows(path: Path, sql: str, params: tuple = ()) -> list[tuple] | str:
    return lib.sqlite_rows(path, sql, params)


def data_root(root: Path) -> Path:
    return root / ".openminion"


def policy_history(root: Path) -> dict[str, Any]:
    db = data_root(root) / "policy" / "policy.db"
    decisions = ro_rows(db, "SELECT tool, method, decision, reason_code, approval_id IS NOT NULL "
                            "FROM policy_decisions ORDER BY created_at, decision_id")
    pending = ro_rows(db, "SELECT tool, method, state, resolution_action, grant_id IS NOT NULL "
                          "FROM policy_pending_confirmations ORDER BY created_at, approval_id")
    grants = ro_rows(db, "SELECT tool, method, effect, duration_type, uses_count, revoked_at IS NOT NULL "
                         "FROM policy_grants ORDER BY created_at, grant_id")
    return {"policy_decisions": decisions, "policy_pending_confirmations": pending, "policy_grants": grants}


def brain_event_counts(root: Path) -> dict[str, int] | str:
    db = data_root(root) / "brain" / "sessions.db"
    rows = ro_rows(db, "SELECT event_type, COUNT(*) FROM session_events GROUP BY event_type ORDER BY event_type")
    return {str(k): int(v) for k, v in rows} if isinstance(rows, list) else rows


def brain_tool_events(root: Path) -> list[dict[str, Any]] | str:
    db = data_root(root) / "brain" / "sessions.db"
    rows = ro_rows(db, "SELECT event_type, payload_json FROM session_events WHERE event_type IN "
                       "('tool.completed', 'tool.call.blocked', 'policy.applied') ORDER BY seq, timestamp")
    if isinstance(rows, str):
        return rows
    out = []
    for event_type, payload in rows:
        try:
            p = json.loads(payload or "{}")
        except ValueError:
            p = {}
        out.append({"event_type": event_type, "status": p.get("status"), "outcome": p.get("outcome"),
                    "explanation": (str(p.get("explanation") or p.get("summary") or "")[:80] or None)})
    return out


def memory_audit(root: Path) -> list[list[Any]] | str:
    db = data_root(root) / "memory" / "memory.audit.db"
    rows = ro_rows(db, "SELECT event_type, target_kind, record_type FROM memory_audit_events "
                       "ORDER BY timestamp, event_id")
    return [list(r) for r in rows] if isinstance(rows, list) else rows


def runtime_event_counts(runtime: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in lib.session_events(runtime.sessions):
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    return dict(sorted(counts.items()))


def candidate_texts(candidate: dict[str, Any]) -> str:
    return " | ".join(json.dumps(candidate.get(k), sort_keys=True, default=str)
                      for k in ("title", "key", "content_json", "meta_json"))


# ---------------------------------------------------------------- PostgreSQL memory schema
class PgMemorySchema:
    """probe_496_lib.PgSchema whose `schema` is the one holding the memory tables (see the module docstring)."""

    def __init__(self, label: str) -> None:
        import probe_496_lib as L

        self._pg = L.PgSchema(label)
        self.sa, self.admin, self.url = self._pg.sa, self._pg.admin, self._pg.url
        self.base = self._pg.schema
        self.derived = f"{self.base}__memory"
        self._located: str | None = None

    def memory_config(self, **kwargs: Any) -> Any:
        return self._pg.memory_config(**kwargs)

    def _find(self) -> list[str]:
        sa = self.sa
        with self.admin.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT table_schema FROM information_schema.tables WHERE table_name = 'memory_candidates' "
                "AND table_schema IN (:a, :b) ORDER BY table_schema"), {"a": self.base, "b": self.derived})
            return [r[0] for r in rows]

    @property
    def schema(self) -> str:
        if self._located is None:
            found = self._find()
            if found:
                self._located = self.derived if self.derived in found else found[0]
        return self._located or self.base

    def layout(self) -> dict[str, Any]:
        """The case's schemas (the configured one and any `<schema>__*`) and their tables, read server-side."""
        sa = self.sa
        with self.admin.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema = :a "
                "OR left(table_schema, :n) = :b ORDER BY 1, 2"),
                {"a": self.base, "b": self.base + "__", "n": len(self.base) + 2})
            out: dict[str, Any] = {}
            for schema, table in rows:
                out.setdefault(schema.replace(self.base, "<case schema>"), []).append(table)
        return {"memory_tables_in": (self.schema or "").replace(self.base, "<case schema>"), "tables": out}

    def drop(self) -> None:
        try:
            with self.admin.begin() as conn:
                conn.execute(self.sa.text(f'DROP SCHEMA IF EXISTS "{self.derived}" CASCADE'))
        finally:
            self._pg.drop()


# ---------------------------------------------------------------- standalone runner
STANDALONE_STATUS_TOOLS = {"t_timeout": "timeout", "t_blocked": "blocked", "t_needs_user": "needs_user",
                           "t_failed": "failed"}


def standalone_runner(root: Path, policy: str) -> tuple[Any, Any, Any, Any]:
    """A BrainRunner from the installed package's local adapters, as the upstream runner tests build it."""
    from openminion.modules.brain.adapters.a2a import LocalA2AAdapter
    from openminion.modules.brain.adapters.context import LocalContextAdapter
    from openminion.modules.brain.adapters.llm import LocalLLMAdapter
    from openminion.modules.brain.adapters.memory import LocalMemoryAdapter
    from openminion.modules.brain.adapters.policy import LocalPolicyAdapter
    from openminion.modules.brain.adapters.session import LocalSessionStore
    from openminion.modules.brain.adapters.tool import LocalToolAdapter
    from openminion.modules.brain.runner import BrainRunner, RunnerOptions
    from openminion.modules.brain.schemas import (AgentBudgets, AgentDefaults, AgentProfile, LLMProfiles,
                                                  PolicyDecision)

    class StatusTools(LocalToolAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[dict[str, Any]] = []

        def list_tools(self) -> list[dict[str, Any]]:
            return super().list_tools() + [{"name": n} for n in ("weather", *STANDALONE_STATUS_TOOLS)]

        def execute(self, *, command: dict[str, Any], session_id: str, trace_id: str) -> dict[str, Any]:
            name = str(command.get("tool_name"))
            self.calls.append({"tool": name, "args": dict(command.get("args") or {})})
            if name in STANDALONE_STATUS_TOOLS:
                return {"status": STANDALONE_STATUS_TOOLS[name], "summary": f"{name} result", "outputs": {},
                        "error": {"code": name.upper(), "message": f"{name} result"}}
            return super().execute(command=command, session_id=session_id, trace_id=trace_id)

    class FixedPolicy:
        contract_version = "v1"

        def __init__(self, outcome: str, explanation: str) -> None:
            self.outcome, self.explanation = outcome, explanation

        def evaluate(self, *, command: Any, working_state: Any, session_context: Any) -> Any:
            return PolicyDecision(outcome=self.outcome, explanation=self.explanation)

    profile = AgentProfile(
        agent_id="standalone-agent", role="general",
        llm_profiles=LLMProfiles(decide_model="decide-default", plan_model="plan-default", act_model=None,
                                 reflect_model="reflect-default", summarize_model="summarize-default"),
        budgets=AgentBudgets(max_ticks_per_user_turn=5, max_tool_calls=3, max_a2a_calls=1,
                             max_total_llm_tokens=1000, max_elapsed_ms=10000),
        defaults=AgentDefaults())
    policy_api = {"allow": LocalPolicyAdapter(),
                  "confirm": FixedPolicy("REQUIRE_CONFIRMATION", "Confirm required"),
                  "deny": FixedPolicy("DENY", "Denied by the probe's policy")}[policy]
    session = LocalSessionStore(root / "sessions")
    memory = LocalMemoryAdapter(root / "memory")
    tools = StatusTools()
    runner = BrainRunner(profile=profile, session_api=session, context_api=LocalContextAdapter(session_store=session),
                         llm_api=LocalLLMAdapter(), tool_api=tools, a2a_api=LocalA2AAdapter(), memory_api=memory,
                         policy_api=policy_api, options=RunnerOptions(metactl_enabled=False))
    return runner, session, memory, tools


def standalone_entries(memory: Any) -> list[dict[str, Any]]:
    path = getattr(memory, "path", None)
    if path is None or not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def sqlite_tables(path: Path) -> list[str]:
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
        return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1")]


__all__ = ["PgMemorySchema", "AGENT_ID", "CAPTURE_MESSAGE", "MEMORY_ARGS", "WEATHER_ARGS", "MEMORY_NEEDLES", "WEATHER_NEEDLES",
           "MESSAGE", "WEATHER_MESSAGE", "TEXT_REPLY", "build_runtime_ext", "ArmedProvider", "HandlerRecorder",
           "model_args", "policy_history", "brain_event_counts", "brain_tool_events", "memory_audit",
           "runtime_event_counts", "candidate_texts", "standalone_runner", "standalone_entries",
           "STANDALONE_STATUS_TOOLS"]
