"""Capture the closure-judge wire traffic for openminion issue #328 (release verification of the installed 0.1.10).

Copied from the earlier om328b/capture_328b.py harness; additions: openminion.__file__ is
printed and recorded, and per-judge-request shape facts (roles, system-message count, tool
output inside the single system message ahead of the Tool-calling contract) are recorded.

Runs the issue's decorated-tool scenario in an isolated OpenMinion home and
records every /chat/completions exchange of the turn: full JSON request body
and full JSON response body. Request headers (Authorization) are never read.
Everything written to disk is passed through MinionProbe's redaction and then
checked for secret shapes; the run aborts instead of writing if any remain.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROBE_REPO = Path(__file__).resolve().parents[3]  # MinionProbe repo root
sys.path.insert(0, str(PROBE_REPO))

from minionprobe.env import PROVIDERS, isolation_env, new_home, setup_agent  # noqa: E402
from minionprobe.fakeprovider import FakeProvider, FakeScript  # noqa: E402

PARTIAL_JUDGE_REPLY = "I do not have access to your order information."


class PartialJudgeFake(FakeProvider):
    """Scripted provider whose closure judge answers with final_answer only."""

    def respond(self, body: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
        choice = body.get("tool_choice")
        forced = choice.get("function", {}).get("name") if isinstance(choice, dict) else None
        text = "\n".join(
            str(m.get("content") or "") for m in body.get("messages") or [] if isinstance(m, dict)
        )
        if forced == "submit_output" and "purpose: judge" in text:
            with self._lock:
                self.requests.append({"model": body.get("model"), "control": True, "judge": True})
            model = str(body.get("model") or "fake-model")
            return 200, self._tool_call_completion(model, "submit_output", {"final_answer": PARTIAL_JUDGE_REPLY}), {}
        return super().respond(body)
from minionprobe.redact import redact_obj  # noqa: E402

_SECRET_SHAPES = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"gsk_[A-Za-z0-9]{16,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"org_[A-Za-z0-9]{8,}"),
    re.compile(r"(?i)authorization"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)x-goog-api-key"),
]


def _ensure_gemini_key() -> None:
    if os.environ.get("GEMINI_API_KEY"):
        return
    if os.name != "nt":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        value, _ = winreg.QueryValueEx(key, "GEMINI_API_KEY")
    if value:
        os.environ["GEMINI_API_KEY"] = str(value)


class WireRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._orig = None

    def install(self) -> None:
        import httpx

        self._orig = httpx.Client.send
        recorder = self
        orig = self._orig

        def send(client: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
            started = time.time()
            response = orig(client, request, *args, **kwargs)
            url = urlparse(str(request.url))
            if url.path.endswith("/chat/completions"):
                try:
                    body = json.loads(request.content or b"{}")
                except ValueError:
                    body = {"_unparsed": True}
                try:
                    response.read()
                    text = response.text
                    try:
                        resp_body: Any = json.loads(text)
                    except ValueError:
                        resp_body = text
                except Exception:  # noqa: BLE001
                    resp_body = None
                with recorder._lock:
                    recorder.calls.append(
                        {
                            "seq": len(recorder.calls),
                            "host": url.hostname or "",
                            "path": url.path,
                            "status": int(response.status_code),
                            "duration_ms": int((time.time() - started) * 1000),
                            "request_body": body,
                            "response_body": resp_body,
                        }
                    )
            return response

        httpx.Client.send = send  # type: ignore[method-assign]

    def uninstall(self) -> None:
        import httpx

        if self._orig is not None:
            httpx.Client.send = self._orig  # type: ignore[method-assign]


def _forced_tool(body: dict[str, Any]) -> str:
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        return str((choice.get("function") or {}).get("name", ""))
    return ""


def _system_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        parts.append(str(content or ""))
    return "\n".join(parts)


def classify(call: dict[str, Any]) -> str:
    body = call["request_body"] if isinstance(call["request_body"], dict) else {}
    text = _system_text(body)
    if _forced_tool(body) == "submit_output":
        if "purpose: judge" in text or "[JUDGE CONTEXT]" in text:
            return "closure_judge"
        if "purpose: reflect" in text:
            return "control_reflect"
        return "control_other"
    return "agent_loop"


def judge_context_lines(call: dict[str, Any]) -> list[str]:
    text = _system_text(call["request_body"])
    start = text.find("[JUDGE CONTEXT]")
    if start < 0:
        return []
    block = text[start:].split("\n\n", 1)[0]
    return block.splitlines()


def check_clean(payload: str) -> list[str]:
    hits = []
    for pattern in _SECRET_SHAPES:
        if pattern.search(payload):
            hits.append(pattern.pattern)
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name, "")
        if len(value) >= 8 and value in payload:
            hits.append(f"literal {name}")
    return hits


def run_once(provider: str, code: str, base: Path, fake: FakeProvider | None, timeout_s: float | None = None) -> dict[str, Any]:
    import openminion
    from openminion import Agent, APIRuntime, tool

    spec = PROVIDERS["local" if provider == "fake" else provider]
    home = new_home(base, f"home-{uuid.uuid4().hex[:6]}")
    setup_agent(
        home,
        spec.agent_id,
        spec,
        base_url=fake.base_url if fake is not None else None,
    )
    os.environ.update(isolation_env(home))
    calls: list[str] = []

    @tool
    def lookup_order(order_id: str) -> str:
        """Return the current status and pickup code for one order."""
        calls.append(order_id)
        return f"order {order_id}: ready for pickup, pickup code {code}"

    recorder = WireRecorder()
    recorder.install()
    runtime = APIRuntime.from_config_path(
        str(home.config_path), home_root=home.root, data_root=home.data_root
    )
    error = ""
    text = ""
    meta: dict[str, Any] = {}
    try:
        with Agent(
            runtime=runtime,
            agent_id=spec.agent_id,
            instructions="Use lookup_order, then answer in one sentence that quotes the pickup code.",
            tools=[lookup_order],
            forced_tools=["lookup_order"],
        ) as agent:
            result = agent.run("Check order A-104", session_id=f"s328-{uuid.uuid4().hex[:6]}", timeout_seconds=timeout_s)
            text = str(getattr(result, "text", "") or "")
            for attr in ("run_state", "status", "tool_calls"):
                value = getattr(result, attr, None)
                if value is not None:
                    meta[attr] = value if isinstance(value, (str, int, float, bool)) else repr(value)[:300]
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"[:600]
    finally:
        recorder.uninstall()
        try:
            runtime.close()
        except Exception:  # noqa: BLE001
            pass
    wire = []
    for call in recorder.calls:
        call = dict(call)
        call["kind"] = classify(call)
        wire.append(call)
    judge_calls = [c for c in wire if c["kind"] == "closure_judge"]
    agent_answers = []
    for c in wire:
        if c["kind"] != "agent_loop" or not isinstance(c["response_body"], dict):
            continue
        for choice in c["response_body"].get("choices") or []:
            content = str(((choice or {}).get("message") or {}).get("content") or "")
            if content:
                agent_answers.append(content)
    judge_lines = judge_context_lines(judge_calls[-1]) if judge_calls else []
    tool_output = f"order A-104: ready for pickup, pickup code {code}"
    judge_shapes = []
    for c in judge_calls:
        body = c["request_body"] if isinstance(c["request_body"], dict) else {}
        msgs = [m for m in body.get("messages") or [] if isinstance(m, dict)]
        systems = [str(m.get("content") or "") for m in msgs if m.get("role") == "system"]
        first = systems[0] if systems else ""
        judge_shapes.append({
            "seq": c["seq"],
            "status": c["status"],
            "roles": [m.get("role") for m in msgs],
            "system_message_count": len(systems),
            "system_has_judge_context": "[JUDGE CONTEXT]" in first,
            "system_has_tool_output": tool_output in first,
            "tool_output_before_contract": tool_output in first and "Tool-calling contract:" in first
            and first.index(tool_output) < first.rindex("Tool-calling contract:"),
        })
    evidence_line = next((ln for ln in judge_lines if ln.startswith("closure_action_outputs:")), "")
    summary_line = next((ln for ln in judge_lines if ln.startswith("action_summary:")), "")
    return {
        "openminion_version": openminion.__version__,
        "openminion_file": openminion.__file__,
        "judge_request_shapes": judge_shapes,
        "provider": provider,
        "model": spec.model,
        "tool_executions": calls,
        "result_text": text,
        "result_meta": meta,
        "error": error,
        "agent_loop_answers": agent_answers,
        "agent_loop_answer_contains_code": any(code in a for a in agent_answers),
        "result_text_contains_code": code in text,
        "judge_call_count": len(judge_calls),
        "judge_context_lines": judge_lines,
        "judge_action_summary_contains_code": code in summary_line,
        "judge_action_outputs_line_present": bool(evidence_line),
        "judge_action_outputs_contains_code": code in evidence_line,
        "wire": wire,
    }


def main() -> int:
    import openminion

    print("openminion", openminion.__version__, openminion.__file__, flush=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["fake", "gemini"], default="fake")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--out", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--fake-final-answer", default=None)
    parser.add_argument("--fake-partial-judge", action="store_true")
    parser.add_argument("--interval", type=float, default=20.0)
    parser.add_argument("--turn-timeout", type=float, default=None)
    parser.add_argument("--code-offset", type=int, default=0)
    parser.add_argument("--start-index", type=int, default=0)
    args = parser.parse_args()

    if args.provider == "gemini":
        _ensure_gemini_key()
        if not os.environ.get("GEMINI_API_KEY"):
            print("GEMINI_API_KEY unavailable", file=sys.stderr)
            return 2
    import logging

    logging.disable(logging.WARNING)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="om328-"))
    fake = None
    if args.provider == "fake":
        provider_cls = PartialJudgeFake if args.fake_partial_judge else FakeProvider
        fake = provider_cls(FakeScript(final_answer=args.fake_final_answer)).start()
    summaries = []
    try:
        for index in range(args.start_index, args.start_index + args.runs):
            if index > args.start_index:
                time.sleep(args.interval)
            code = f"ZEBRA-{42 + index + args.code_offset}" if args.provider == "gemini" else "ZEBRA-42"
            started = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            record = run_once(args.provider, code, base, fake, args.turn_timeout)
            record["turn_timeout_s"] = args.turn_timeout
            record["started"] = started
            record["run_index"] = index
            record["pickup_code"] = code
            sanitized = redact_obj(record)
            payload = json.dumps(sanitized, indent=2, ensure_ascii=False)
            hits = check_clean(payload)
            if hits:
                print(f"run {index}: refusing to write, secret-like content: {hits}", file=sys.stderr)
                return 3
            path = out / f"{args.label}-run{index}.json"
            path.write_text(payload + "\n", encoding="utf-8")
            brief = {k: sanitized[k] for k in (
                "openminion_version", "openminion_file", "model", "tool_executions", "result_text", "error",
                "agent_loop_answer_contains_code", "result_text_contains_code", "judge_call_count",
                "judge_action_summary_contains_code", "judge_action_outputs_line_present",
                "judge_action_outputs_contains_code",
            )}
            brief["file"] = path.name
            brief["http_status"] = [c["status"] for c in sanitized["wire"]]
            brief["kinds"] = [c["kind"] for c in sanitized["wire"]]
            summaries.append(brief)
            print(json.dumps(brief, ensure_ascii=False))
    finally:
        if fake is not None:
            fake.stop()
    (out / f"{args.label}-summary.json").write_text(
        json.dumps(summaries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
