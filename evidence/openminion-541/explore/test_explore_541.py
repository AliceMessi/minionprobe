"""Exploration for #541 (not part of the PR): prints OBS lines per scenario."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from openminion import Agent, APIRuntime
from openminion.base.config import (
    AgentProfileConfig,
    ConfigManager,
    OpenMinionConfig,
    save_config,
)
from tests._csc_fixtures import _csc_install_default_agent
from tests.runtime.test_turn_capture_authorization import (
    AGENT_ID,
    _completion,
    _events,
    _Provider,
    _runtime,
    _text,
)

ARGS_A = {
    "record_type": "fact",
    "key": "fact:editor_font",
    "title": "Editor font",
    "content": "The user writes code in Iosevka 14.",
}
ARGS_B = {
    "record_type": "fact",
    "key": "fact:editor_theme",
    "title": "Editor theme",
    "content": "The user prefers a light editor theme.",
}
MSG_A = "Remember that I write code in Iosevka 14."
MSG_B = "Remember that I prefer a light editor theme."
META = (
    "thread_id",
    "thread_decision_action",
    "thread_decision_reason",
    "respond_kind",
    "pending_confirmation_command_id",
    "brain_status",
)


class _ArmedProvider(_Provider):
    def __init__(self) -> None:
        super().__init__()
        self.armed: list[dict[str, Any]] = []
        self.calls = 0
        self.last_user: list[str] = []

    def respond(self, body: dict[str, Any]) -> dict[str, Any]:
        names = [tool["function"]["name"] for tool in body.get("tools") or []]
        choice = body.get("tool_choice")
        forced = isinstance(choice, dict)
        msgs = body.get("messages") or []
        users = [m for m in msgs if m.get("role") == "user"]
        self.last_user.append(str((users[-1].get("content") if users else ""))[-120:])
        if not forced and "memory_write" in names and self.armed:
            args = self.armed.pop(0)
            self.calls += 1
            with self._lock:
                self.requests.append("tool_call")
            call = {
                "id": f"call-memory-write-{self.calls}",
                "type": "function",
                "function": {"name": "memory_write", "arguments": json.dumps(args)},
            }
            return _completion(
                {"role": "assistant", "content": None, "tool_calls": [call]},
                "tool_calls",
            )
        return super().respond(body)


@pytest.fixture
def provider() -> Any:
    server = _ArmedProvider()
    try:
        yield server
    finally:
        server.close()


def _handler(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    handled: list[Any] = []
    spec = runtime.tools.get("memory.write")
    handler = spec.handler

    def recording(args: dict[str, Any], ctx: Any) -> Any:
        handled.append(
            {
                "key": args.get("key"),
                "session": getattr(ctx, "session_id", None),
                "agent": getattr(ctx, "agent_id", None),
            }
        )
        return handler(args, ctx)

    monkeypatch.setattr(spec, "handler", recording)
    return handled


def _obs(label: str, result: Any, handled: list[Any], **extra: Any) -> None:
    row = {"label": label, "text": (result.text or "")[:90], "run_state": result.run_state,
           "session": result.session_id, "result_thread": getattr(result, "thread_id", "n/a"),
           "handled": list(handled), **{k: result.metadata.get(k) for k in META}, **extra}
    print("OBS " + json.dumps(row, sort_keys=True))


def _agent(runtime: Any, **kw: Any) -> Agent:
    return Agent(runtime=runtime, forced_tools=["memory.write"], **kw)


def test_basic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: _ArmedProvider) -> None:
    runtime = _runtime(tmp_path, monkeypatch, provider, constraint=None)
    try:
        handled = _handler(runtime, monkeypatch)
        payloads: list[Any] = []
        run_turn = runtime.run_turn

        def rec(*, payload: Any, **kw: Any) -> Any:
            payloads.append(json.loads(json.dumps(payload)))
            return run_turn(payload=payload, **kw)

        monkeypatch.setattr(runtime, "run_turn", rec)
        agent = _agent(runtime, session_id="s1")
        provider.armed.append(ARGS_A)
        first = agent.run(MSG_A)
        _obs("first", first, handled, payload=payloads[-1]["inbound_metadata"])
        for tid in (None, "", "  "):
            r = agent.run("yes", thread_id=tid)
            _obs(f"plain-yes-{tid!r}", r, handled, payload=payloads[-1]["inbound_metadata"])
        r = agent.run("yes", thread_id=first.thread_id)
        _obs("explicit-yes", r, handled, payload=payloads[-1]["inbound_metadata"])
        r = agent.run("yes", thread_id=first.thread_id)
        _obs("explicit-yes-again", r, handled)
        print("LASTUSER", json.dumps(provider.last_user[-6:]))
    finally:
        runtime.close()


def test_deny_cancel_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: _ArmedProvider) -> None:
    runtime = _runtime(tmp_path, monkeypatch, provider, constraint=None)
    try:
        handled = _handler(runtime, monkeypatch)
        agent = _agent(runtime, session_id="s1")
        provider.armed.append(ARGS_A)
        first = agent.run(MSG_A)
        _obs("first", first, handled)
        r = agent.run("no", thread_id=first.thread_id)
        _obs("explicit-no", r, handled)
        r = agent.run("yes", thread_id=first.thread_id)
        _obs("explicit-yes-after-no", r, handled)
        provider.armed.append(ARGS_B)
        second = agent.run(MSG_B, session_id="s2")
        _obs("second", second, handled)
        cancel = Event()
        cancel.set()
        try:
            r = agent.run("yes", session_id="s2", thread_id=second.thread_id, cancel_event=cancel)
            _obs("pre-cancelled-yes", r, handled)
        except Exception as exc:  # noqa: BLE001
            print("OBS " + json.dumps({"label": "pre-cancelled-yes", "exc": f"{type(exc).__name__}: {exc}", "handled": handled}))
        r = agent.run("yes", session_id="s2", thread_id=second.thread_id)
        _obs("yes-after-cancel", r, handled)
    finally:
        runtime.close()


def test_multi_and_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: _ArmedProvider) -> None:
    runtime = _runtime(tmp_path, monkeypatch, provider, constraint=None)
    try:
        handled = _handler(runtime, monkeypatch)
        agent = _agent(runtime, session_id="s1")
        provider.armed.append(ARGS_A)
        a = agent.run(MSG_A)
        _obs("A", a, handled)
        provider.armed.append(ARGS_B)
        b = agent.run(MSG_B)
        _obs("B same session", b, handled)
        r = agent.run("yes", thread_id=b.thread_id)
        _obs("yes-B", r, handled)
        r = agent.run("yes", thread_id=a.thread_id)
        _obs("yes-A", r, handled)
        # isolation
        provider.armed.append(ARGS_A)
        c = agent.run(MSG_A, session_id="sx")
        _obs("C sx", c, handled)
        provider.armed.append(ARGS_B)
        d = agent.run(MSG_B, session_id="sy")
        _obs("D sy", d, handled)
        r = agent.run("yes", session_id="sy", thread_id=c.thread_id)
        _obs("yes sy with C thread", r, handled)
        r = agent.run("yes", session_id="sz", thread_id=c.thread_id)
        _obs("yes new sz with C thread", r, handled)
        other = _agent(runtime)
        r = other.run("yes", session_id="sx", thread_id=d.thread_id)
        _obs("yes sx with D thread (other Agent)", r, handled)
        r = agent.run("yes", session_id="sx", thread_id=c.thread_id)
        _obs("yes sx C own", r, handled)
        r = agent.run("yes", session_id="sy", thread_id=d.thread_id)
        _obs("yes sy D own", r, handled)
    finally:
        runtime.close()


def _two_agents(root: Path, monkeypatch: pytest.MonkeyPatch, provider: _Provider) -> Any:
    monkeypatch.setenv("OPENMINION_HOME", str(root))
    monkeypatch.setenv("OPENMINION_DATA_ROOT", str(root / ".openminion"))
    monkeypatch.setenv("CAPTURE_AUTHZ_PROVIDER_KEY", "local-dummy-key")
    config = OpenMinionConfig()
    _csc_install_default_agent(config, provider="openai")
    config.agents["helper"] = AgentProfileConfig(
        name="helper", provider="openai", default_channel="console"
    )
    config.providers.openai.api_key_env = "CAPTURE_AUTHZ_PROVIDER_KEY"
    config.providers.openai.base_url = provider.base_url
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / ".openminion" / "state.db")
    save_config(config, str(root / "agents.json"))
    manager = ConfigManager.load(
        str(root / "agents.json"), home_root=root, data_root=root / ".openminion"
    )
    return APIRuntime.from_manager(manager)


def test_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: _ArmedProvider) -> None:
    runtime = _two_agents(tmp_path, monkeypatch, provider)
    try:
        handled = _handler(runtime, monkeypatch)
        owner = _agent(runtime, agent_id=AGENT_ID, session_id="s1")
        helper = _agent(runtime, agent_id="helper", session_id="s1")
        provider.armed.append(ARGS_A)
        a = owner.run(MSG_A)
        _obs("owner A", a, handled)
        try:
            r = helper.run("yes", thread_id=a.thread_id)
            _obs("helper yes same session owner thread", r, handled)
        except Exception as exc:  # noqa: BLE001
            print("OBS " + json.dumps({"label": "helper-yes", "exc": f"{type(exc).__name__}: {exc}", "handled": handled}))
        provider.armed.append(ARGS_B)
        h = helper.run(MSG_B, session_id="s2")
        _obs("helper B s2", h, handled)
        try:
            r = owner.run("yes", session_id="s2", thread_id=h.thread_id)
            _obs("owner yes on helper thread s2", r, handled)
        except Exception as exc:  # noqa: BLE001
            print("OBS " + json.dumps({"label": "owner-yes-helper", "exc": f"{type(exc).__name__}: {exc}", "handled": handled}))
        r = owner.run("yes", thread_id=a.thread_id)
        _obs("owner yes own", r, handled)
        r = helper.run("yes", session_id="s2", thread_id=h.thread_id)
        _obs("helper yes own", r, handled)
    finally:
        runtime.close()


def test_stream_and_callback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: _ArmedProvider) -> None:
    runtime = _runtime(tmp_path, monkeypatch, provider, constraint=None)
    try:
        handled = _handler(runtime, monkeypatch)
        agent = _agent(runtime, session_id="s1")
        provider.armed.append(ARGS_A)
        first = agent.run(MSG_A)
        events: list[Any] = []
        r = agent.run_stream("yes", thread_id=first.thread_id, on_delta=events.append)
        _obs("stream-yes", r, handled, n_events=len(events),
             kinds=[type(e).__name__ + ":" + str(getattr(e, "kind", getattr(e, "type", e if isinstance(e, str) else "")))[:60] for e in events][:40])
        print("EVENTS", json.dumps([repr(e)[:200] for e in events][:30]))
        events2: list[Any] = []
        r = agent.run_stream("yes", thread_id=first.thread_id, on_delta=events2.append)
        _obs("stream-yes-again", r, handled, n_events=len(events2))
        # callback on an explicit thread
        approvals: list[Any] = []

        def cb(name: str, args: dict[str, Any], _aid: str) -> bool:
            approvals.append((name, args.get("key")))
            return False

        provider.armed.append(ARGS_B)
        r = agent.run(MSG_B, thread_id=first.thread_id, approval_callback=cb)
        _obs("callback-deny explicit thread", r, handled, approvals=approvals)

        def cb2(name: str, args: dict[str, Any], _aid: str) -> bool:
            approvals.append((name, args.get("key"), "approve"))
            return True

        provider.armed.append(ARGS_B)
        r = agent.run(MSG_B, thread_id=first.thread_id, approval_callback=cb2)
        _obs("callback-approve explicit thread", r, handled, approvals=approvals)
        # pending call, then explicit yes with a denying callback
        provider.armed.append(ARGS_A)
        p = agent.run(MSG_A, session_id="s3")
        _obs("pending s3", p, handled)
        r = agent.run("yes", session_id="s3", thread_id=p.thread_id, approval_callback=cb)
        _obs("explicit yes with deny cb", r, handled, approvals=approvals)
        print("PROMPTS", len(_events(runtime, "policy_confirmation_prompt")))
    finally:
        runtime.close()


def test_round2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: _ArmedProvider) -> None:
    runtime = _two_agents(tmp_path, monkeypatch, provider)
    try:
        handled = _handler(runtime, monkeypatch)
        owner = _agent(runtime, agent_id=AGENT_ID, session_id="s1")
        helper = _agent(runtime, agent_id="helper", session_id="h1")
        provider.armed.append(ARGS_A)
        a = owner.run(MSG_A)
        _obs("owner A", a, handled)
        r = helper.run("yes", thread_id=a.thread_id)
        _obs("helper own session + owner thread", r, handled)
        r = helper.run("yes", session_id="h-new", thread_id=a.thread_id)
        _obs("helper new session + owner thread", r, handled)
        approvals: list[Any] = []

        def cb(name: str, args: dict[str, Any], _aid: str) -> bool:
            approvals.append((name, args.get("key")))
            return True

        r = owner.run("yes", thread_id=a.thread_id, approval_callback=cb)
        _obs("owner explicit yes approve cb", r, handled, approvals=approvals)
        plain = owner.run("Hello there.", session_id="s5")
        _obs("plain s5", plain, handled)
        provider.armed.append(ARGS_B)
        r = owner.run(MSG_B, session_id="s5", thread_id=plain.thread_id, approval_callback=cb)
        _obs("new call explicit thread approve cb", r, handled, approvals=approvals)
        events: list[Any] = []
        provider.armed.append(ARGS_A)
        p = owner.run(MSG_A, session_id="s6")
        r = owner.run_stream("yes", session_id="s6", thread_id=p.thread_id, on_delta=lambda e: events.append((len(handled), e)))
        _obs("stream", r, handled, n=len(events),
             tool=[(n, e.get("kind"), e.get("tool_name")) for n, e in events if isinstance(e, dict)],
             phases=[(n, type(e).__module__ + "." + type(e).__name__, getattr(e, "status_key", None)) for n, e in events if not isinstance(e, dict)])
    finally:
        runtime.close()
