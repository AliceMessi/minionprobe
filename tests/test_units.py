"""Fast unit tests: no network, no OpenMinion runtime."""

from __future__ import annotations

import json
import urllib.request

import pytest

from minionprobe.core import ERROR_TEXT_RE, REGISTRY, BLOCKS
from minionprobe.fakeprovider import FakeProvider, FakeScript, Fault, fill_from_schema
from minionprobe.recorder import classify_call, parse_duration, summarize, HttpEvent
from minionprobe.redact import redact, redact_obj
from minionprobe.report import render_matrix
from minionprobe import probes  # noqa: F401  (registers probes)


def test_redact_scrubs_org_ids_and_key_shapes(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "literal-secret-value-123456")
    text = (
        "Rate limit for org_01abcdefghijklmnop; key gsk_ABCDEFGHIJKLMNOPQRSTUV; "
        "google AIzaSyA1234567890abcdefghijklmnop; Bearer abcdefghijklmnop; literal-secret-value-123456"
    )
    out = redact(text)
    assert "org_01abc" not in out and "org_REDACTED" in out
    assert "gsk_" not in out
    assert "AIza" not in out
    assert "abcdefghijklmnop" not in out
    assert "literal-secret-value-123456" not in out


def test_redact_obj_is_recursive():
    data = {"a": ["org_0123456789abcdef", {"b": "org_0123456789abcdef"}], "n": 3}
    assert json.dumps(redact_obj(data)).count("org_REDACTED") == 2


@pytest.mark.parametrize(
    "raw,seconds",
    [("40.612s", 40.612), ("577ms", 0.577), ("1m26.4s", 86.4), ("2", 2.0), ("", 0.0)],
)
def test_parse_duration_handles_groq_formats(raw, seconds):
    assert parse_duration(raw) == pytest.approx(seconds)


def test_classify_call_separates_control_calls():
    forced = {"type": "function", "function": {"name": "submit_output"}}
    assert classify_call(["submit_output"], forced) == "control"
    assert classify_call(["lookup_order"], "auto") == "agent_loop"
    assert classify_call([], None) == "agent_loop"


def test_error_text_regex_matches_runtime_error_bodies():
    assert ERROR_TEXT_RE.match("groq: RATE_LIMITED: groq rate limited: ...").group(1) == "RATE_LIMITED"
    assert ERROR_TEXT_RE.match("gemini: PONG-ABC123") is None
    assert ERROR_TEXT_RE.match("Order A-104 is ready: CODE") is None


def test_fill_from_schema_respects_refs_enums_and_semantics():
    schema = {
        "type": "object",
        "required": ["satisfied", "next_action", "final_answer", "mode", "items", "child"],
        "properties": {
            "satisfied": {"type": "boolean"},
            "next_action": {"enum": ["continue", "close", "replan"]},
            "final_answer": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "mode": {"enum": ["a", "b"]},
            "items": {"type": "array", "items": {"type": "string"}},
            "child": {"$ref": "#/$defs/Child"},
        },
        "$defs": {"Child": {"type": "object", "properties": {"n": {"type": "integer", "minimum": 2}}, "required": ["n"]}},
    }
    value = fill_from_schema(schema, overrides={"final_answer": "done"})
    assert value == {"satisfied": True, "next_action": "close", "final_answer": "done", "mode": "a", "items": [], "child": {"n": 2}}


def _post(url: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - localhost
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_fake_provider_tool_call_then_answer_then_fault():
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}}]
    with FakeProvider(FakeScript(tool_args={"lookup": {"id": "A-1"}})) as fake:
        url = fake.base_url + "/chat/completions"
        status, first = _post(url, {"model": "m", "messages": [{"role": "user", "content": "hi"}], "tools": tools})
        call = first["choices"][0]["message"]["tool_calls"][0]["function"]
        assert status == 200 and call["name"] == "lookup" and json.loads(call["arguments"]) == {"id": "A-1"}
        messages = [{"role": "user", "content": "hi"}, {"role": "tool", "content": "result-42"}]
        status, second = _post(url, {"model": "m", "messages": messages, "tools": tools})
        assert status == 200 and "result-42" in second["choices"][0]["message"]["content"]
        fake.script.faults = [Fault(status=429, when="after_tool", times=1)]
        status, third = _post(url, {"model": "m", "messages": messages, "tools": tools})
        assert status == 429 and third["error"]["code"] == "rate_limit_exceeded"
        assert len(fake.requests) == 3


def test_every_block_has_at_least_one_probe():
    covered = {d.block for d in REGISTRY.values()}
    assert covered == {block for block, _ in BLOCKS}


def test_render_matrix_lists_providers_and_failures():
    payload = {
        "openminion_version": "0.1.8",
        "python": "3.12",
        "platform": "test",
        "started": "s",
        "finished": "f",
        "providers": {"groq": {"model": "m1", "note": ""}, "gemini": {"model": "m2", "note": ""}},
        "results": [
            {"provider": "groq", "probe": "tools.decorated_tool", "block": "tools", "title": "T", "status": "PASS", "latency_s": 3.2, "checks": [], "observations": [], "known_issues": [], "error": ""},
            {"provider": "gemini", "probe": "tools.decorated_tool", "block": "tools", "title": "T", "status": "FAIL", "latency_s": 9.0,
             "checks": [{"name": "final answer quotes the tool result", "ok": False, "detail": "x"}], "observations": [], "known_issues": ["#284"], "error": ""},
        ],
    }
    md = render_matrix(payload)
    assert "| Tools | `decorated_tool`: T | **PASS** 3s | **FAIL** 9s (#284) |" in md
    assert "failed check: final answer quotes the tool result" in md


def test_summarize_counts_status_and_kind():
    events = [
        HttpEvent(0, 0.0, 200, "h", "/v1/chat/completions", "m", ["submit_output"], None, 2, False, "control"),
        HttpEvent(1, 0.0, 429, "h", "/v1/chat/completions", "m", ["t"], "auto", 3, True, "agent_loop", error="slow down"),
    ]
    summary = summarize(events)
    assert summary["by_status"] == {"200": 1, "429": 1}
    assert summary["by_kind"] == {"control": 1, "agent_loop": 1}
    assert summary["errors"][0]["error"] == "slow down"


def test_rule_triage_maps_blocked_known_and_verified():
    from minionprobe.triage import rule_triage

    base = {"provider": "groq", "title": "t", "checks": [], "error": "", "known_issues": []}
    assert rule_triage(base | {"probe": "tools.decorated_tool", "status": "BLOCKED"}).category == "provider_limitation"
    known = rule_triage(base | {"probe": "delegation.handoff", "status": "FAIL", "known_issues": ["#284"]})
    assert known.category == "known_issue" and known.known_ref == "#284"
    verified = rule_triage(base | {"probe": "sessions.followup_not_replayed", "status": "FAIL"})
    assert verified.category == "openminion_bug" and "02-followup" in verified.rationale
