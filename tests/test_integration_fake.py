"""Integration tests: the real OpenMinion runtime against the local fake provider.

Offline and credential-free. Each test builds an isolated OpenMinion home with
``openminion setup --provider custom-openai-compatible`` pointed at the fake
server. The tests assert on MinionProbe's harness behaviour; where a probe
documents an OpenMinion bug we assert the *current* 0.1.8 behaviour and mark
the expectation with the issue draft it belongs to, so a fix upstream shows up
as an xpass/failure here and the matrix can be updated.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from minionprobe.core import PASS, FAIL, REGISTRY, ProbeContext, execute
from minionprobe.env import PROVIDERS, isolation_env
from minionprobe.fakeprovider import FakeProvider, FakeScript
from minionprobe.recorder import RECORDER
from minionprobe.runner import _prepare_local_home, _runtime_factory

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def local_ctx(tmp_path_factory):
    RECORDER.install()
    fake = FakeProvider(FakeScript()).start()
    base = tmp_path_factory.mktemp("homes")
    home = _prepare_local_home(base, fake)
    saved = {k: os.environ.get(k) for k in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT")}
    os.environ.update(isolation_env(home))
    ctx = ProbeContext(
        provider=PROVIDERS["local"], home=home, run_id="pytest", out_dir=Path(base),
        runtime_factory=_runtime_factory(home), log=lambda _msg: None, extras={"fake": fake},
    )
    yield ctx
    ctx.close()
    fake.stop()
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_basic_turn_through_runtime_records_http_evidence(local_ctx):
    from openminion import Agent

    agent = Agent(runtime=local_ctx.runtime, agent_id="local")
    result, record = local_ctx.turn(agent, "hello fake", label="t", session_id=local_ctx.session("t"))
    assert result is not None and result.run_state == "completed"
    assert "FAKE-ANSWER(fake-model)" in result.text
    assert record.http["requests"] >= 1
    assert record.http["hosts"] == ["127.0.0.1"]
    assert record.http["models"] == ["fake-model"]


def test_decorated_tool_probe_passes_on_fake_provider(local_ctx):
    """The fake calls the first offered tool and echoes its result, so every
    harness check in tools.decorated_tool must hold (proves #248/#249 fixed)."""
    result = execute(REGISTRY["tools.decorated_tool"], local_ctx)
    failed = [c for c in result.checks if not c.ok]
    assert result.status == PASS, failed


def test_followup_replay_probe_reproduces_issue_draft_02(local_ctx):
    """issue-drafts/02: follow-up Agent.run on the same session replays the
    first reply. Expected FAIL on openminion 0.1.8."""
    result = execute(REGISTRY["sessions.followup_not_replayed"], local_ctx)
    assert result.status == FAIL
    assert any(c.name == "follow-up 2 reached the provider" and not c.ok for c in result.checks)


def test_rate_limit_after_tool_probe_reproduces_issue_draft_04(local_ctx):
    """issue-drafts/04: a 429 after a tool call is returned as reply text with
    run_state=completed. Expected FAIL on openminion 0.1.8."""
    result = execute(REGISTRY["diagnostics.rate_limit_after_tool"], local_ctx)
    assert result.status == FAIL
    assert any(o["kind"] == "provider_error_returned_as_text" for o in result.observations)


def test_unreachable_provider_raises_provider_error(local_ctx):
    result = execute(REGISTRY["diagnostics.provider_unreachable"], local_ctx)
    assert result.status == PASS, result.checks
