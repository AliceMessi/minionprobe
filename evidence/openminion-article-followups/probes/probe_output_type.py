"""What `Agent(output_type=<pydantic model>)` sends to the model, and what it returns for a prose reply.

Installed package only (no repo test fixtures); run with `python -I -X utf8`
from an empty directory outside any checkout. It reuses the #448 probe's
helpers (`probe_420_lib.py`, `_common.py`, unchanged): a real `APIRuntime` for
one agent, `openminion`, whose `openai` provider is a scripted
OpenAI-compatible server on 127.0.0.1. Every request the runtime sends is
recorded: its tool names, `tool_choice`, `response_format`, top-level keys,
and whether the output model's class name or its unique field descriptions
appear anywhere in the request body.

The agent mirrors the HackerNoon "Go-Live Gate" judge: no `tools=` argument,
`instructions` that ask for JSON, `output_type=ZqLaunchReport`, and a user
message that does NOT carry the schema. Each case is a fresh runtime home.
  A  the model answers a markdown table in prose (the article's failure)
  B  control: the model answers one schema-conforming JSON object
  W  control: the same JSON object wrapped in prose (the documented 0.1.x
     compatibility recovery, API_COMPATIBILITY.md)
Forced `submit_output` control calls (fact extraction, freshness, closure
judge, final-answer repair) are answered by the #448 server, except that the
judge/repair `final_answer` echoes the case's model answer unchanged, so the
closure path cannot rewrite it (that is openminion#720, not this probe).

Checks:
  defect checks (exit 1 if any fails): A raises AgentOutputValidationError with
  the raw prose and a pydantic ValidationError, and returns no result; B and W
  return a validated ZqLaunchReport instance.
  mechanism checks (confirm ANALYSIS.md; a FAIL means the analysis is wrong):
  no main-turn request carries `response_format`, a forced `tool_choice`, a tool
  derived from the output model, or the schema text.

Usage: python -I -X utf8 probe.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()


def _dist(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _direct_url() -> Any:
    try:
        text = metadata.distribution("openminion").read_text("direct_url.json")
        return json.loads(text) if text else None
    except Exception:  # noqa: BLE001
        return None


info = banner("probe_output_type",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic")},
              openminion_direct_url=_direct_url())

from pydantic import BaseModel, Field, ValidationError  # noqa: E402

from openminion import Agent, AgentOutputValidationError  # noqa: E402

import probe_420_lib as lib  # noqa: E402
from probe_420_lib import ScriptedProvider, build_runtime, chdir_env, restore_env  # noqa: E402

# Markers that exist only in the output model's JSON schema, never in the
# instructions, the user message or the scripted answers.
DESC_SITE = "zqmarker-site-7f3a the audited host"
DESC_VERDICT = "zqmarker-verdict-91c2 launch decision"
DESC_ROWS = "zqmarker-rows-c0de one row per check"
SCHEMA_MARKERS = ("ZqLaunchReport", "ZqCheckRow", "zqmarker-")


class ZqCheckRow(BaseModel):
    name: str
    status: Literal["pass", "warn", "fail"]


class ZqLaunchReport(BaseModel):
    site: str = Field(description=DESC_SITE)
    verdict: Literal["ready", "ready_with_warnings", "blocked"] = Field(description=DESC_VERDICT)
    checks: list[ZqCheckRow] = Field(description=DESC_ROWS)


INSTRUCTIONS = ("You are a strict pre-launch reviewer. Judge the facts you receive. "
                "Reply with exactly one JSON object and nothing else.")
MESSAGE = "Facts for example.com: title is 63 characters; canonical present; sitemap OK; missing page answers 404."
GOOD = {"site": "example.com", "verdict": "ready",
        "checks": [{"name": "title", "status": "pass"}, {"name": "real_404", "status": "pass"}]}
ANSWERS = {
    "A": ("Here is the launch review for example.com:\n\n"
          "| check | status | evidence |\n|---|---|---|\n"
          "| title | pass | 63 characters |\n| canonical | pass | present |\n"
          "| sitemap | pass | HTTP 200 |\n| real_404 | pass | 404 |\n\n**Verdict: ready**"),
    "B": json.dumps(GOOD),
    "W": "Sure, here is the report: " + json.dumps(GOOD) + " Let me know if you need more.",
}
c = Checks()


def _submit_properties(body: dict[str, Any]) -> list[str]:
    for tool in body.get("tools") or []:
        fn = (tool.get("function") or {}) if isinstance(tool, dict) else {}
        if fn.get("name") == "submit_output":
            return sorted(((fn.get("parameters") or {}).get("properties") or {}).keys())
    return []


class OutputTypeProvider(ScriptedProvider):
    """The #448 scripted server: every non-forced call answers the case's model text;
    forced judge/repair calls echo that text as `final_answer`."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.log: list[dict[str, Any]] = []
        super().__init__()

    def respond(self, body: dict[str, Any], path: str) -> dict[str, Any]:
        raw = json.dumps(body, ensure_ascii=False)
        choice = body.get("tool_choice")
        forced = str((choice.get("function") or {}).get("name", "")) if isinstance(choice, dict) else ""
        txt = lib._all_text(body)
        entry = {
            "index": len(self.log),
            "role": "control (forced submit_output)" if forced == "submit_output" else "main turn",
            "body_keys": sorted(body.keys()),
            "tools": [str((t.get("function") or {}).get("name", "")) for t in body.get("tools") or []
                      if isinstance(t, dict)],
            "tool_choice": choice,
            "response_format": body.get("response_format"),
            "submit_output_properties": _submit_properties(body),
            "schema_markers_found": [m for m in SCHEMA_MARKERS if m in raw],
            "user_message_present": MESSAGE in txt,
            "instructions_present": INSTRUCTIONS in txt,
        }
        if forced == "submit_output":
            base = super().respond(body, path)
            entry["kind"] = self.requests[-1]["kind"] if self.requests else None
            props = entry["submit_output_properties"]
            if entry["kind"] == "judge" or "final_answer" in props:
                args: dict[str, Any] = {"final_answer": self.answer}
                if entry["kind"] == "judge" or "satisfied" in props:
                    args.update({"satisfied": True, "next_action": "close", "reason": "Answer matches the task."})
                base = lib._submit(args, f"call-echo-{entry['index']}")
            entry["reply"] = json.loads(base["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
            self.log.append(entry)
            return base
        with self.lock:
            self.requests.append({"index": len(self.requests), "kind": "main_text", "path": path,
                                  "model": body.get("model"), "mentions_capture_message": False,
                                  "t": time.monotonic()})
        entry["kind"] = "main_text"
        entry["reply"] = self.answer
        self.log.append(entry)
        return lib._text(self.answer)


def run_case(label: str) -> dict[str, Any]:
    root = WORK / label
    provider = OutputTypeProvider(ANSWERS[label])
    old_env = chdir_env(root)
    row: dict[str, Any] = {"case": label, "model_answer": ANSWERS[label]}
    runtime = None
    try:
        runtime = build_runtime(root, provider)
        try:
            with Agent(runtime=runtime, instructions=INSTRUCTIONS, output_type=ZqLaunchReport,
                       session_id=f"output-type-{label}") as agent:
                res = agent.run(MESSAGE, timeout_seconds=120)
            row["result"] = {"returned": True, "output_type": type(res.output).__name__,
                             "output": res.output.model_dump() if isinstance(res.output, BaseModel) else res.output,
                             "text": res.text, "run_state": res.run_state, "run_id": res.run_id}
        except AgentOutputValidationError as exc:
            row["result"] = {"returned": False, "exception": "AgentOutputValidationError", "message": str(exc)[:600],
                             "raw_text": exc.raw_text,
                             "validation_error_is_pydantic": isinstance(exc.validation_error, ValidationError)}
        except Exception as exc:  # noqa: BLE001 - recorded
            row["result"] = {"returned": False, "exception": type(exc).__name__, "message": str(exc)[:600]}
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        row["case_error"] = f"{type(exc).__name__}: {exc}"
        row["case_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                    for f in traceback.extract_tb(exc.__traceback__)][-8:]
    finally:
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:  # noqa: BLE001
                row["runtime_close"] = f"{type(exc).__name__}: {exc}"
        restore_env(old_env)
        provider.close()
    row["requests"] = provider.log
    return row


cases = {label: run_case(label) for label in ("A", "B", "W")}


def main_requests(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in case.get("requests", []) if r.get("role") == "main turn"]


def mechanism(case: dict[str, Any]) -> dict[str, Any]:
    mains = main_requests(case)
    return {"main_requests": len(mains),
            "response_format": [r["response_format"] for r in mains if r.get("response_format") is not None],
            "tool_choice": [r["tool_choice"] for r in mains if r.get("tool_choice") is not None],
            "tool_names": sorted({t for r in mains for t in r.get("tools", [])}),
            "schema_markers_any_request": sorted({m for r in case.get("requests", [])
                                                  for m in r.get("schema_markers_found", [])}),
            "user_message_reached_model": any(r.get("user_message_present") for r in mains),
            "instructions_reached_model": any(r.get("instructions_present") for r in mains)}


a, b, w = cases["A"].get("result") or {}, cases["B"].get("result") or {}, cases["W"].get("result") or {}
c.check("A (defect check): a markdown-table reply raises AgentOutputValidationError carrying the raw prose and a "
        "pydantic ValidationError; no AgentRunResult (no silent text result with run_state=completed)",
        a.get("returned") is False and a.get("exception") == "AgentOutputValidationError"
        and a.get("raw_text") == ANSWERS["A"] and a.get("validation_error_is_pydantic") is True, a)
c.check("B (control): a schema-conforming JSON reply returns AgentRunResult.output as a validated ZqLaunchReport",
        b.get("returned") is True and b.get("output_type") == "ZqLaunchReport" and b.get("output") == GOOD, b)
c.check("W (control, documented 0.1.x recovery): one schema-valid JSON object wrapped in prose is recovered",
        w.get("returned") is True and w.get("output_type") == "ZqLaunchReport" and w.get("output") == GOOD, w)

mech = {label: mechanism(case) for label, case in cases.items()}
all_mains = [r for case in cases.values() for r in main_requests(case)]
c.check("Mechanism: the user message and the instructions reach the model in every case",
        all(m["user_message_reached_model"] and m["instructions_reached_model"] for m in mech.values()), mech)
c.check("Mechanism (matches ANALYSIS): no main-turn request carries `response_format` or a forced `tool_choice`",
        bool(all_mains) and not any(m["response_format"] or m["tool_choice"] for m in mech.values()),
        {k: {"response_format": v["response_format"], "tool_choice": v["tool_choice"]} for k, v in mech.items()})
c.check("Mechanism (matches ANALYSIS): no request in any case carries the output model's name or its field "
        "descriptions (the schema is never sent: not as a tool, not as response_format, not as prompt text)",
        not any(m["schema_markers_any_request"] for m in mech.values()),
        {k: v["schema_markers_any_request"] for k, v in mech.items()})
hosts = sorted({h for case in cases.values() for h in case.get("provider_hosts", [])})
errors = {k: case.get("case_error") for k, case in cases.items() if case.get("case_error")}
c.check("Offline provider and no case error: every case's openai base URL is the scripted server on 127.0.0.1",
        hosts == ["127.0.0.1"] and not errors, {"hosts": hosts, "errors": errors})

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": {"mechanism": mech, "cases": cases}})
summary = c.summary()
print("main-turn tools offered:", json.dumps({k: v["tool_names"] for k, v in mech.items()})[:1200])
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
