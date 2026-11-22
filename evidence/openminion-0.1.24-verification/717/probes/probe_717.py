"""#717 published-package probe: Retry-After admission on the installed path.

    python -I -X utf8 probe_717.py <work_dir> <out.json>      # exit 0 only if every check passes

Installed package only (no repo test fixtures, no checkout on sys.path), run
from an empty directory. Every provider is `RateLimitStub` on 127.0.0.1 (see
probe_717_lib.py): no external network, no real key (the key variable holds a
placeholder string). Real clock, real HTTP, real sleeps: hints are 2-5 s.

Part A (cases A1-A9): `LLMCTL.from_config` with the bundled `openai` provider
pointed at the stub, so each request goes through the installed transport
(HTTP 429 + `Retry-After` header -> `LLMCtlError` facts), the installed retry
loop and the installed `routing_defaults` primary/fallback routing. A turn
deadline and cancellation come from the installed `await_with_cancel` (the
function the runtime's turn ingress uses); "no deadline" is a call outside it.
Each case records the stub's requests (server-side monotonic time), the
response or exception, the ChatPhaseTimer provider attempts and the
`openminion-llm` telemetry operation counts (installed TelemetryService).

Part B (cases B1-B3): a full installed runtime turn: `APIRuntime.from_manager`
with provider `openai` on the stub (harness `build_runtime` from the #420/#745
probes, byte copy), `Agent(runtime=...).run(message, timeout_seconds=...)`.
The runtime builds its own LLMCTL (max_retries 2, backoff 300 ms, no routing
fallbacks) and the turn has a finite deadline, so B covers the entry loop's
route admission and the inner retry as the runtime wires them. The stub
answers the turn's `decide` requests with 429 as scripted.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import traceback
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402
from probe_717_lib import USAGE, RateLimitStub  # noqa: E402

from openminion.modules.llm import LLMCTL  # noqa: E402
from openminion.modules.runtime.sync import await_with_cancel  # noqa: E402
from openminion.modules.telemetry.service import TelemetryCtl, TelemetryService  # noqa: E402
from openminion.modules.telemetry.trace.phase_timing import (  # noqa: E402
    ChatPhaseTimer,
    use_chat_phase_timer,
)

KEY_ENV = "PROBE_717_PROVIDER_KEY"
SLACK = 0.02  # Windows monotonic granularity is ~15.6 ms
USAGE_SEEN = {"input_tokens": USAGE["prompt_tokens"], "output_tokens": USAGE["completion_tokens"],
              "total_tokens": USAGE["total_tokens"]}


def http_date_in(seconds: int) -> tuple[str, float]:
    target = int(time.time()) + seconds + 1  # whole seconds, at least `seconds` ahead
    return format_datetime(datetime.fromtimestamp(target, tz=timezone.utc), usegmt=True), float(target)


def llmctl(stub: RateLimitStub, telemetry: Any, *, max_retries: int, backoff_ms: int,
           fallback_on: tuple[str, ...] | None = None) -> LLMCTL:
    global_cfg: dict[str, Any] = {
        "default_provider": "openai",
        "default_model": "primary-model",
        "retries": {"max_retries": max_retries, "backoff_ms": backoff_ms},
    }
    if fallback_on is not None:
        global_cfg["routing_defaults"] = {
            "primary": {"provider": "openai", "model": "primary-model"},
            "fallbacks": [{"provider": "openai", "model": "fallback-model", "on": list(fallback_on)}],
        }
    return LLMCTL.from_config(
        {
            "version": 1,
            "llmctl": global_cfg,
            "providers": {"openai": {"base_url": stub.base_url, "api_key_env": KEY_ENV}},
            "agents": {"default": {"tool_policy": {"enable_tools": False}}},
        },
        telemetryctl=telemetry,
    )


def payload(session_id: str) -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": "probe 717"}],
        "metadata": {"session_id": session_id, "turn_id": "turn-1", "trace_id": "trace-717",
                     "llm_call_id": "call-717", "purpose": "entry"},
    }


def run_llm_case(work: Path, stub: RateLimitStub, name: str, scripts: dict[str, list], *,
                 timeout_s: float | None, max_retries: int = 1, backoff_ms: int = 100,
                 fallback_on: tuple[str, ...] | None = None, cancel_after: float | None = None) -> dict[str, Any]:
    stub.reset(scripts)
    session_id = f"sess-717-{name}"
    db = work / f"{name}-telemetry.db"

    async def go() -> dict[str, Any]:
        service = TelemetryService(str(db))
        try:
            rt = llmctl(stub, TelemetryCtl(service), max_retries=max_retries, backoff_ms=backoff_ms,
                        fallback_on=fallback_on)
            cancel = threading.Event() if cancel_after is not None else None
            timer = threading.Timer(cancel_after, cancel.set) if cancel is not None else None
            call = rt.client(agent_name="default").call(payload(session_id))
            started = time.monotonic()
            result: dict[str, Any] = {"started": started}
            if timer is not None:
                timer.start()
            try:
                if timeout_s is None:
                    response = await call
                else:
                    response = await await_with_cancel(call, timeout_s=timeout_s, cancel_event=cancel)
                result["response"] = response
            except BaseException as exc:  # noqa: BLE001 - the probe reports it
                result["exception"] = f"{type(exc).__name__}: {exc}"
            result["ended"] = time.monotonic()
            if timer is not None:
                timer.join()
            summary = await service.get_module_summary(session_id)
            result["operation_counts"] = dict((summary.get("openminion-llm") or {}).get("operation_counts") or {})
            return result
        finally:
            await service.close()

    phase = ChatPhaseTimer()
    with use_chat_phase_timer(phase):
        result = asyncio.run(go())
    response = result.pop("response", None)
    started = result.pop("started")
    entries = stub.entries()
    for e in entries:
        e["t_rel"] = round(e.pop("t") - started, 3)
    view: dict[str, Any] = {
        "case": name,
        "timeout_s": timeout_s,
        "max_retries": max_retries,
        "backoff_ms": backoff_ms,
        "fallback_on": list(fallback_on) if fallback_on is not None else None,
        "elapsed_s": round(result["ended"] - started, 3),
        "requests": entries,
        "attempts": [
            {k: item.get(k) for k in ("route_posture", "attempt_posture", "outcome", "error_code", "provider", "model")}
            for item in phase.build_payload().provider_attempts
        ],
        "operation_counts": result["operation_counts"],
        "exception": result.get("exception"),
    }
    if response is not None:
        view["response"] = {
            "ok": response.ok,
            "provider": response.provider,
            "model": response.model,
            "output_text": response.output_text,
            "usage": {k: getattr(response.usage, k, None) for k in ("input_tokens", "output_tokens", "total_tokens")}
            if response.usage is not None else None,
            "error_code": response.error.code if response.error is not None else None,
            "error_details": dict(response.error.details or {}) if response.error is not None else None,
        }
    return view


def gaps(view: dict[str, Any], model: str = "primary-model") -> list[float]:
    times = [e["t_rel"] for e in view["requests"] if e["model"] == model]
    return [round(b - a, 3) for a, b in zip(times, times[1:])]


def statuses(view: dict[str, Any]) -> list[tuple[str, int]]:
    return [(e["model"], e["status"]) for e in view["requests"]]


def run_turn_case(work: Path, stub: RateLimitStub, name: str, *, hint: str, limited: int,
                  timeout_s: float) -> dict[str, Any]:
    from openminion import Agent
    from probe_420_lib import build_runtime

    root = work / name
    runtime = build_runtime(root, stub)  # type: ignore[arg-type]
    stub.reset({})
    stub.rate_limit_first("decide", limited, hint)
    started = time.monotonic()
    view: dict[str, Any] = {"case": name, "hint": hint, "timeout_seconds": timeout_s, "limited_decide_requests": limited}
    try:
        with Agent(runtime=runtime, instructions="Answer briefly.", session_id=f"sess-717-{name}") as agent:
            res = agent.run("What is two plus two?", timeout_seconds=timeout_s)
        view["reply"] = str(getattr(res, "text", "") or "")
        view["exception"] = None
    except BaseException as exc:  # noqa: BLE001 - the probe reports it
        view["reply"] = None
        view["exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        view["elapsed_s"] = round(time.monotonic() - started, 3)
        runtime.close()
    entries = stub.entries()
    for e in entries:
        e["t_rel"] = round(e.pop("t") - started, 3)
    view["requests"] = entries
    return view


def main() -> int:
    work = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    quiet()
    clear_override_env()
    os.environ[KEY_ENV] = "probe-not-a-key"
    os.environ["PROBE_420_PROVIDER_KEY"] = "probe-not-a-key"
    info = banner("probe_717")
    checks = Checks()
    cases: dict[str, Any] = {}
    errors: dict[str, str] = {}
    stub = RateLimitStub()
    url = f"{stub.base_url}/chat/completions"

    def facts(hint: str) -> dict[str, Any]:
        return {"provider": "openai", "url": url, "status_code": 429, "upstream_code": "rate_limit_exceeded",
                "upstream_message": "Rate limit reached for probe-717 requests per minute.",
                "upstream_type": "requests", "request_id": "req-probe-717-0", "retry_after": hint}

    def case(key: str, fn) -> dict[str, Any] | None:
        try:
            cases[key] = fn()
            return cases[key]
        except Exception:  # noqa: BLE001 - recorded
            errors[key] = traceback.format_exc()
            print(f"ERROR in {key}:\n{errors[key]}", flush=True)
            return None

    # ---------------------------------------------------------------- A1 numeric hint that fits
    v = case("A1_numeric_fits", lambda: run_llm_case(
        work, stub, "A1", {"primary-model": [("429", "2"), ("ok", "primary after hint")]}, timeout_s=30))
    if v:
        g = gaps(v)
        r = v.get("response") or {}
        checks.check(
            "A1 numeric Retry-After: 2 that fits a 30 s turn deadline: the retry is sent no earlier than 2 s after "
            "the 429 (server-side gap), then succeeds; one 429 and one 200 on the primary route",
            statuses(v) == [("primary-model", 429), ("primary-model", 200)] and len(g) == 1 and g[0] >= 2.0 - SLACK
            and r.get("ok") is True and r.get("output_text") == "primary after hint",
            {"statuses": statuses(v), "gap_s": g, "response": r})
        checks.check(
            "A1 usage and attempt accounting after a hinted wait: usage is the provider's (11/5/16); attempts "
            "primary/initial/error then primary/retry/ok; telemetry request 2, error 1, retry 1, response 1",
            r.get("usage") == USAGE_SEEN
            and [(a["route_posture"], a["attempt_posture"], a["outcome"]) for a in v["attempts"]]
            == [("primary", "initial", "error"), ("primary", "retry", "ok")]
            and {k: v["operation_counts"].get(k) for k in ("request", "error", "retry", "response")}
            == {"request": 2, "error": 1, "retry": 1, "response": 1},
            {"usage": r.get("usage"), "attempts": v["attempts"], "operation_counts": v["operation_counts"]})

    # ---------------------------------------------------------------- A2 HTTP-date hint that fits
    v = case("A2_http_date_fits", lambda: run_llm_case(
        work, stub, "A2", {"primary-model": [("429", "date+2"), ("ok", "primary after date")]}, timeout_s=30))
    if v:
        g = gaps(v)
        r = v.get("response") or {}
        req = v["requests"]
        late = round(req[1]["wall"] - req[0]["hint_epoch"], 3) if len(req) == 2 and req[0]["hint_epoch"] else None
        checks.check(
            "A2 HTTP-date Retry-After (the next whole second >= 2 s after the 429) that fits a 30 s turn deadline: "
            "the retry is not sent before that date (server wall clock) nor less than 2 s after the 429, and "
            "succeeds on the primary route with the provider's usage",
            statuses(v) == [("primary-model", 429), ("primary-model", 200)] and len(g) == 1 and g[0] >= 2.0 - SLACK
            and late is not None and late >= -SLACK
            and r.get("ok") is True and r.get("output_text") == "primary after date" and r.get("usage") == USAGE_SEEN,
            {"hint": req[0]["retry_after"] if req else None, "statuses": statuses(v), "gap_s": g,
             "resend_after_date_s": late, "response": r})

    # ---------------------------------------------------------------- A3 no deadline: refuse the wait
    v = case("A3_no_deadline", lambda: run_llm_case(
        work, stub, "A3", {"primary-model": [("429", "2"), ("ok", "must not be sent")]}, timeout_s=None))
    if v:
        r = v.get("response") or {}
        checks.check(
            "A3 numeric hint with no turn deadline (call outside await_with_cancel), max_retries=1: no wait and no "
            "resend; the route's RATE_LIMITED error is returned with the transport's facts unchanged",
            statuses(v) == [("primary-model", 429)] and r.get("ok") is False and r.get("error_code") == "RATE_LIMITED"
            and r.get("error_details") == facts("2") and v["elapsed_s"] < 1.0,
            {"statuses": statuses(v), "elapsed_s": v["elapsed_s"], "response": r})
        checks.check(
            "A3 failed-attempt accounting of a refused wait: one attempt primary/initial/error (RATE_LIMITED); "
            "telemetry request 1, one HTTP POST, error 1, no retry operation",
            [(a["route_posture"], a["attempt_posture"], a["outcome"], a["error_code"]) for a in v["attempts"]]
            == [("primary", "initial", "error", "RATE_LIMITED")]
            and v["operation_counts"] == {"request": 1, "http_json_post": 1, "error": 1},
            {"attempts": v["attempts"], "operation_counts": v["operation_counts"]})

    # ---------------------------------------------------------------- A4 too long for the deadline
    v = case("A4_too_long", lambda: run_llm_case(
        work, stub, "A4", {"primary-model": [("429", "30"), ("ok", "must not be sent")]}, timeout_s=5))
    if v:
        r = v.get("response") or {}
        checks.check(
            "A4 numeric hint 30 s with a 5 s turn deadline, max_retries=1: refused without waiting or resending; "
            "RATE_LIMITED with the transport's facts unchanged; telemetry request 1, one HTTP POST, error 1",
            statuses(v) == [("primary-model", 429)] and r.get("ok") is False and r.get("error_code") == "RATE_LIMITED"
            and r.get("error_details") == facts("30") and v["elapsed_s"] < 1.0
            and v["operation_counts"] == {"request": 1, "http_json_post": 1, "error": 1},
            {"statuses": statuses(v), "elapsed_s": v["elapsed_s"], "response": r,
             "operation_counts": v["operation_counts"]})

    # ---------------------------------------------------------------- A5 fallback when the primary cannot wait
    for key, timeout in (("A5_fallback_too_long", 5.0), ("A5_fallback_no_deadline", None)):
        v = case(key, lambda timeout=timeout, key=key: run_llm_case(
            work, stub, key, {"primary-model": [("429", "30"), ("ok", "must not be sent")],
                              "fallback-model": [("ok", "from fallback")]},
            timeout_s=timeout, fallback_on=("RATE_LIMITED",)))
        if v:
            r = v.get("response") or {}
            checks.check(
                f"{key}: primary hint 30 s cannot wait ({'5 s deadline' if timeout else 'no deadline'}), "
                "fallback configured on RATE_LIMITED: no wait, the primary is not resent, the fallback answers "
                "with its own usage",
                statuses(v) == [("primary-model", 429), ("fallback-model", 200)] and r.get("ok") is True
                and r.get("model") == "fallback-model" and r.get("output_text") == "from fallback"
                and r.get("usage") == USAGE_SEEN and v["elapsed_s"] < 1.0,
                {"statuses": statuses(v), "elapsed_s": v["elapsed_s"], "response": r, "attempts": v["attempts"]})

    # ---------------------------------------------------------------- A6 fallback filter kept
    v = case("A6_fallback_filter", lambda: run_llm_case(
        work, stub, "A6", {"primary-model": [("429", "30")], "fallback-model": [("ok", "must not be sent")]},
        timeout_s=None, fallback_on=("PROVIDER_ERROR",)))
    if v:
        r = v.get("response") or {}
        checks.check(
            "A6 fallback configured only on PROVIDER_ERROR: a refused RATE_LIMITED wait does not reach the "
            "fallback; the primary's error and facts are returned",
            statuses(v) == [("primary-model", 429)] and r.get("error_code") == "RATE_LIMITED"
            and r.get("error_details") == facts("30"),
            {"statuses": statuses(v), "response": r})

    # ---------------------------------------------------------------- A7 a wait that fits keeps the primary
    v = case("A7_fit_before_fallback", lambda: run_llm_case(
        work, stub, "A7", {"primary-model": [("429", "2"), ("ok", "primary after hint")],
                           "fallback-model": [("ok", "must not be sent")]},
        timeout_s=30, fallback_on=("RATE_LIMITED",)))
    if v:
        g = gaps(v)
        r = v.get("response") or {}
        checks.check(
            "A7 hint 2 s fits a 30 s deadline with a RATE_LIMITED fallback configured: the primary is retried after "
            "the hint and answers; the fallback receives no request",
            statuses(v) == [("primary-model", 429), ("primary-model", 200)] and len(g) == 1 and g[0] >= 2.0 - SLACK
            and r.get("model") == "primary-model" and r.get("ok") is True,
            {"statuses": statuses(v), "gap_s": g, "response": r})

    # ---------------------------------------------------------------- A8 cancellation during the wait
    v = case("A8_cancel_during_wait", lambda: run_llm_case(
        work, stub, "A8", {"primary-model": [("429", "5"), ("ok", "must not be sent")]},
        timeout_s=30, cancel_after=0.5))
    if v:
        checks.check(
            "A8 hint 5 s that fits a 30 s deadline, turn cancelled 0.5 s in: the hinted wait stops (the call raises "
            "within 1.5 s) and no retry request is sent",
            statuses(v) == [("primary-model", 429)] and v.get("exception") is not None and "response" not in v
            and v["elapsed_s"] < 1.5,
            {"statuses": statuses(v), "elapsed_s": v["elapsed_s"], "exception": v.get("exception"),
             "response": v.get("response")})

    # ---------------------------------------------------------------- A9 hints without a future delay
    past, _ = http_date_in(-3600)
    for label, hint in (("zero", "0"), ("past_date", past), ("malformed", "soon"), ("missing", None)):
        key = f"A9_{label}"
        v = case(key, lambda hint=hint, key=key: run_llm_case(
            work, stub, key, {"primary-model": [("429", hint), ("ok", "after backoff")]},
            timeout_s=None, backoff_ms=300))
        if v:
            g = gaps(v)
            r = v.get("response") or {}
            checks.check(
                f"{key}: Retry-After {hint!r} (no future delay), no deadline: ordinary backoff (300 ms) is kept, the "
                "retry is sent and succeeds",
                statuses(v) == [("primary-model", 429), ("primary-model", 200)] and len(g) == 1
                and 0.3 - SLACK <= g[0] < 1.0 and r.get("ok") is True,
                {"statuses": statuses(v), "gap_s": g, "response": r})

    # ---------------------------------------------------------------- B: full runtime turns
    def decide_requests(view: dict[str, Any]) -> list[dict[str, Any]]:
        return [e for e in view["requests"] if e["kind"] == "decide"]

    v = case("B1_turn_numeric_fits", lambda: run_turn_case(work, stub, "B1", hint="2", limited=1, timeout_s=60))
    if v:
        d = decide_requests(v)
        gap = round(d[1]["t_rel"] - d[0]["t_rel"], 3) if len(d) >= 2 else None
        checks.check(
            "B1 runtime turn (Agent.run, timeout 60 s), first decide request answered 429 Retry-After: 2: the "
            "runtime resends it no earlier than 2 s later and the turn completes with a reply",
            len(d) >= 2 and [e["status"] for e in d[:2]] == [429, 200] and gap is not None and gap >= 2.0 - SLACK
            and v["exception"] is None and bool(v["reply"]),
            {"decide": [(e["status"], e["t_rel"]) for e in d], "gap_s": gap, "reply": v["reply"],
             "exception": v["exception"], "elapsed_s": v["elapsed_s"]})

    v = case("B2_turn_http_date_fits", lambda: run_turn_case(
        work, stub, "B2", hint="date+2", limited=1, timeout_s=60))
    if v:
        d = decide_requests(v)
        gap = round(d[1]["t_rel"] - d[0]["t_rel"], 3) if len(d) >= 2 else None
        late = round(d[1]["wall"] - d[0]["hint_epoch"], 3) if len(d) >= 2 and d[0]["hint_epoch"] else None
        checks.check(
            "B2 runtime turn, first decide request answered 429 with an HTTP-date Retry-After (the next whole second "
            ">= 2 s ahead): resent not before that date nor less than 2 s later, and the turn completes with a reply",
            len(d) >= 2 and [e["status"] for e in d[:2]] == [429, 200] and gap is not None and gap >= 2.0 - SLACK
            and late is not None and late >= -SLACK and v["exception"] is None and bool(v["reply"]),
            {"hint": d[0]["retry_after"] if d else None, "decide": [(e["status"], e["t_rel"]) for e in d],
             "gap_s": gap, "resend_after_date_s": late, "reply": v["reply"], "exception": v["exception"]})

    v = case("B3_turn_too_long", lambda: run_turn_case(work, stub, "B3", hint="600", limited=99, timeout_s=30))
    if v:
        d = decide_requests(v)
        exc = v["exception"] or ""
        checks.check(
            "B3 runtime turn (timeout 30 s), every decide request answered 429 Retry-After: 600: one decide request "
            "only (no inner retry, no entry-loop resend before the hint), the turn ends promptly with the "
            "provider's RATE_LIMITED error carrying its message and request ID",
            len(d) == 1 and d[0]["status"] == 429 and "RATE_LIMITED" in exc
            and "Rate limit reached for probe-717 requests per minute." in exc
            and f"req-probe-717-{d[0]['index']}" in exc and v["elapsed_s"] < 10,
            {"decide": [(e["status"], e["t_rel"]) for e in d], "exception": exc[:600], "elapsed_s": v["elapsed_s"]})

    # ---------------------------------------------------------------- offline and error-free
    hosts = sorted({stub.base_url.split("/")[2].split(":")[0]})
    checks.check(
        "Offline and error-free: every provider is the stub on 127.0.0.1 and no case raised outside the call "
        "under test",
        hosts == ["127.0.0.1"] and not errors,
        {"hosts": hosts, "errors": {k: v.splitlines()[-1] for k, v in errors.items()}})

    stub.close()
    summary = checks.summary()
    write_result(out, {"banner": info, "checks": checks.items, "summary": summary, "cases": cases, "errors": errors})
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}", flush=True)
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
