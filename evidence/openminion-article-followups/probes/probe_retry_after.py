"""A 429 with `Retry-After` is retried before the time the provider gave.

Installed package only (no repo test fixtures); run with `python -I -X utf8`
from an empty directory outside any checkout. It reuses the #448 probe's
helpers (`probe_420_lib.py`, `_common.py`, unchanged): a real `APIRuntime` for
one agent, `openminion`, whose `openai` provider points at a scripted
OpenAI-compatible server on 127.0.0.1. Here the server answers some requests
with HTTP 429, a Groq-style body and a `Retry-After` header (RFC 9110 10.2.3),
and the rest with the scripted 200 answers of `ScriptedProvider.respond`.

Each case is a fresh runtime home and one `Agent(runtime=...).run(...)` turn
with a 120 s turn timeout:
  A  count-limited: the first 2 requests get 429 + `Retry-After: 2`, then 200.
     A client that honours the header sends request 2 >= 2 s after request 1.
  B  window-limited, like a per-minute token limit: every request in the 8 s
     after the first one gets 429 + `Retry-After: <seconds left>`; afterwards
     200. A client that honours the header waits once and the turn succeeds.

Call-through recorders (they call the original, never change a result) log the
retry sleeps of the two retry owners: `modules/llm/runtime/flow.py`
(`_call_with_retries`, `time.sleep`) and `modules/brain/loop/orchestration.py`
(`_BACKOFF_SLEEP`).

Usage: python -I -X utf8 probe.py <work_dir> <out.json>
Exit 1 when a check fails (the defect is present).
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
from pathlib import Path
from typing import Any

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


info = banner("probe_retry_after",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "pydantic", "httpx")},
              openminion_direct_url=_direct_url())

from openminion import Agent  # noqa: E402

from probe_420_lib import ScriptedProvider, build_runtime, chdir_env, restore_env  # noqa: E402

QUESTION = "Say ok."
TURN_TIMEOUT_S = 120
CASE_A_LIMITED, CASE_A_RETRY_AFTER = 2, 2
CASE_B_WINDOW_S = 8.0
TOLERANCE_S = 0.05  # clock and scheduling slack when comparing a gap with Retry-After
c = Checks()


# ---------------------------------------------------------------- recorders
SLEEPS: list[dict[str, Any]] = []


class _TimeProxy:
    """Stands in for the `time` module inside llm/runtime/flow.py; records sleep()."""

    def __init__(self, real: Any, owner: str) -> None:
        self._real, self._owner = real, owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def sleep(self, seconds: float) -> None:
        SLEEPS.append({"owner": self._owner, "seconds": round(float(seconds), 3), "t": time.monotonic()})
        self._real.sleep(seconds)


def install_sleep_recorders() -> dict[str, str]:
    installed: dict[str, str] = {}
    try:
        import openminion.modules.llm.runtime.flow as flow

        flow.time = _TimeProxy(flow.time, "llm/runtime/flow.py:_call_with_retries")  # type: ignore[assignment]
        installed["flow"] = "ok"
    except Exception as exc:  # noqa: BLE001
        installed["flow"] = f"{type(exc).__name__}: {exc}"
    try:
        import openminion.modules.brain.loop.orchestration as orch

        original = orch._BACKOFF_SLEEP

        def backoff_sleep(seconds: float) -> None:
            SLEEPS.append({"owner": "brain/loop/orchestration.py:_BACKOFF_SLEEP", "seconds": round(float(seconds), 3),
                           "t": time.monotonic()})
            original(seconds)

        orch._BACKOFF_SLEEP = backoff_sleep
        installed["orchestration"] = "ok"
    except Exception as exc:  # noqa: BLE001
        installed["orchestration"] = f"{type(exc).__name__}: {exc}"
    return installed


RECORDERS = install_sleep_recorders()


# ---------------------------------------------------------------- rate-limited provider
class RateLimitedProvider(ScriptedProvider):
    """The #448 probe's scripted answers behind a 429 gate.

    mode "count": the first `limited` requests get 429 + `Retry-After: <retry_after>`.
    mode "window": every request within `window_s` of the first one gets 429 +
    `Retry-After: <ceil(seconds left)>` (a per-minute limit that resets at a known time).
    """

    def __init__(self, *, mode: str, limited: int = 0, retry_after: int = 0, window_s: float = 0.0) -> None:
        # ScriptedProvider.__init__ is not called: its handler always answers 200.
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.gate = threading.Lock()
        self.mode, self.limited, self.retry_after, self.window_s = mode, limited, retry_after, window_s
        self.arrivals: list[dict[str, Any]] = []
        self.first_t: float | None = None
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status, payload, headers = provider.answer(body, self.path)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - /v1/models, if anything asks
                data = json.dumps({"object": "list", "data": [{"id": "fixture-model", "object": "model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def answer(self, body: dict[str, Any], path: str) -> tuple[int, dict[str, Any], dict[str, str]]:
        with self.gate:
            now = time.monotonic()
            if self.first_t is None:
                self.first_t = now
            index = len(self.arrivals)
            success = self.respond(body, path)  # classifies the request (records its kind)
            kind = self.requests[-1]["kind"] if self.requests else "unknown"
            retry_after: int | None = None
            if self.mode == "count" and index < self.limited:
                retry_after = self.retry_after
            elif self.mode == "window" and now - self.first_t < self.window_s:
                retry_after = max(1, math.ceil(self.window_s - (now - self.first_t)))
            row = {"n": index + 1, "kind": kind, "t": now, "status": 429 if retry_after is not None else 200}
            if retry_after is not None:
                row["retry_after_s"] = retry_after
                row["advertised_ready_t"] = now + retry_after
            self.arrivals.append(row)
        if retry_after is None:
            return 200, success, {}
        message = (f"Rate limit reached for model `fixture-model` on tokens per minute (TPM): Limit 6000, "
                   f"Used 5900, Requested 900. Please try again in {retry_after}s.")
        return 429, {"error": {"message": message, "type": "tokens", "code": "rate_limit_exceeded"}}, {
            "Retry-After": str(retry_after),
            "x-ratelimit-limit-tokens": "6000",
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": f"{retry_after}s",
        }


# ---------------------------------------------------------------- cases
def _error_facts(exc: BaseException) -> dict[str, Any]:
    details = getattr(exc, "details", None)
    details = dict(details) if isinstance(details, dict) else {}
    chain = []
    cur: BaseException | None = exc
    while cur is not None and len(chain) < 5:
        chain.append({"type": type(cur).__name__, "code": getattr(cur, "code", None),
                      "retry_after": (getattr(cur, "details", None) or {}).get("retry_after")
                      if isinstance(getattr(cur, "details", None), dict) else None})
        cur = cur.__cause__ or cur.__context__
    return {"type": type(exc).__name__, "module": type(exc).__module__, "code": getattr(exc, "code", None),
            "message": str(exc)[:300], "details_retry_after": details.get("retry_after"),
            "details_status_code": details.get("status_code"), "details_reason_code": details.get("reason_code"),
            "details_keys": sorted(details), "cause_chain": chain}


def run_case(label: str, provider: RateLimitedProvider) -> dict[str, Any]:
    root = WORK / label
    old_env = chdir_env(root)
    row: dict[str, Any] = {"case": label, "mode": provider.mode}
    runtime = None
    mark_sleep = len(SLEEPS)
    try:
        runtime = build_runtime(root, provider)
        start = time.monotonic()
        try:
            with Agent(runtime=runtime) as agent:
                res = agent.run(QUESTION, session_id=f"retry-after-{label}", timeout_seconds=TURN_TIMEOUT_S)
            row["outcome"] = {"ok": True, "run_state": getattr(res, "run_state", None),
                              "text": str(getattr(res, "text", ""))[:200]}
        except Exception as exc:  # noqa: BLE001 - recorded
            row["outcome"] = {"ok": False, "error": _error_facts(exc)}
        row["turn_seconds"] = round(time.monotonic() - start, 3)
        t0 = provider.arrivals[0]["t"] if provider.arrivals else start
        row["requests"] = [{**{k: v for k, v in r.items() if k not in ("t", "advertised_ready_t")},
                            "at_s": round(r["t"] - t0, 3),
                            **({"advertised_ready_at_s": round(r["advertised_ready_t"] - t0, 3)}
                               if "advertised_ready_t" in r else {})}
                           for r in provider.arrivals]
        row["sleeps"] = [{"owner": s["owner"], "seconds": s["seconds"], "at_s": round(s["t"] - t0, 3)}
                         for s in SLEEPS[mark_sleep:]]
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
    return row


def early_retries(arrivals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Requests sent before the time the previous 429's Retry-After allowed."""
    out = []
    for prev, cur in zip(arrivals, arrivals[1:]):
        if prev["status"] == 429:
            gap = cur["t"] - prev["t"]
            if gap + TOLERANCE_S < prev["retry_after_s"]:
                out.append({"request": cur["n"], "after_429_request": prev["n"], "gap_s": round(gap, 3),
                            "retry_after_s": prev["retry_after_s"]})
    return out


prov_a = RateLimitedProvider(mode="count", limited=CASE_A_LIMITED, retry_after=CASE_A_RETRY_AFTER)
case_a = run_case("A", prov_a)
arr_a = prov_a.arrivals
prov_b = RateLimitedProvider(mode="window", window_s=CASE_B_WINDOW_S)
case_b = run_case("B", prov_b)
arr_b = prov_b.arrivals

# ---------------------------------------------------------------- checks
hosts = sorted({h for case in (case_a, case_b) for h in case.get("provider_hosts", [])})
errors = {case["case"]: case.get("case_error") for case in (case_a, case_b) if case.get("case_error")}
c.check("Offline provider and no case error: the openai base URL is the scripted server on 127.0.0.1, and the "
        "sleep recorders are installed on both retry owners",
        hosts == ["127.0.0.1"] and not errors and set(RECORDERS.values()) == {"ok"},
        {"hosts": hosts, "errors": errors, "recorders": RECORDERS})

early_a = early_retries(arr_a)
c.check(f"A. every request that follows a 429 with `Retry-After: {CASE_A_RETRY_AFTER}` is sent at least "
        f"{CASE_A_RETRY_AFTER} s after it (RFC 9110 10.2.3)",
        len(arr_a) >= 2 and arr_a[0]["status"] == 429 and not early_a,
        {"early_retries": early_a, "requests": case_a.get("requests"), "sleeps": case_a.get("sleeps")})
c.check(f"A (control). after the first {CASE_A_LIMITED} limited answers the turn completes with the scripted reply "
        "(the retry path itself works)",
        bool((case_a.get("outcome") or {}).get("ok")), case_a.get("outcome"))

inside_b = [r for r in arr_b[1:] if r["t"] - arr_b[0]["t"] < CASE_B_WINDOW_S] if arr_b else []
c.check(f"B. no request is sent before the time the first 429 advertised (`Retry-After: "
        f"{arr_b[0].get('retry_after_s') if arr_b else '?'}`, an {CASE_B_WINDOW_S:.0f} s rate window)",
        bool(arr_b) and arr_b[0]["status"] == 429 and not inside_b,
        {"requests_inside_window": len(inside_b), "early_retries": early_retries(arr_b),
         "requests": case_b.get("requests"), "sleeps": case_b.get("sleeps")})
c.check(f"B. the turn succeeds: the provider accepts calls {CASE_B_WINDOW_S:.0f} s after the first one, well inside "
        f"the {TURN_TIMEOUT_S} s turn timeout",
        bool((case_b.get("outcome") or {}).get("ok")),
        {"outcome": case_b.get("outcome"), "turn_seconds": case_b.get("turn_seconds"),
         "last_request_at_s": (case_b.get("requests") or [{}])[-1].get("at_s")})
out_b = case_b.get("outcome") or {}
err_b = out_b.get("error") or {}
c.check("B (facts). if the turn fails, it fails with the typed ProviderError code RATE_LIMITED "
        "(openminion#326/#338) and the error still carries the provider's Retry-After value in details.retry_after",
        bool(out_b.get("ok")) or (err_b.get("type") == "ProviderError" and err_b.get("code") == "RATE_LIMITED"
                                  and bool(err_b.get("details_retry_after"))),
        err_b or out_b)

observed = {"case_A": case_a, "case_B": case_b, "recorders": RECORDERS}
write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": observed})
for case in (case_a, case_b):
    print(f"case {case['case']} requests:")
    for r in case.get("requests") or []:
        extra = f"  429 Retry-After: {r['retry_after_s']}" if r["status"] == 429 else "  200"
        print(f"  #{r['n']:>2} at {r['at_s']:7.3f}s  {r['kind']:<16}{extra}")
    for s in case.get("sleeps") or []:
        print(f"  sleep {s['seconds']:.3f}s at {s['at_s']:7.3f}s  by {s['owner']}")
    print(f"  outcome: {json.dumps(case.get('outcome'), default=str)[:400]}")
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
