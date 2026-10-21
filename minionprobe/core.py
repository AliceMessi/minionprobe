"""Probe model, registry and the per-provider execution context."""

from __future__ import annotations

import re
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .env import ProbeHome, ProviderSpec
from .recorder import RECORDER, HttpEvent, HttpRecorder, parse_duration, summarize
from .redact import redact_obj, shorten

PASS, FAIL, ERROR, BLOCKED, SKIP = "PASS", "FAIL", "ERROR", "BLOCKED", "SKIP"

BLOCKS = [
    ("agent_execution", "Agent execution"),
    ("models", "Models / providers"),
    ("tools", "Tools"),
    ("permissions", "Permissions"),
    ("sessions", "Sessions"),
    ("memory", "Memory"),
    ("skills", "Skills"),
    ("tasks", "Tasks / scheduling"),
    ("delegation", "Delegation"),
    ("diagnostics", "Diagnostics"),
]
BLOCK_TITLES = dict(BLOCKS)

# "<agent>: <CODE>: <message>" is how 0.1.8 renders some provider failures as
# the reply body instead of raising ProviderError.
ERROR_TEXT_RE = re.compile(r"^\s*[\w.\-]+:\s+([A-Z][A-Z_]{3,}):\s")
RETRY_IN_RE = re.compile(r"try again in ([0-9.]+(?:ms|s|m))", re.IGNORECASE)
RATE_LIMIT_CODES = {"RATE_LIMITED", "QUOTA_EXCEEDED", "PROVIDER_RATE_LIMITED"}


class ProbeBlocked(Exception):
    """The provider (not OpenMinion) prevented the probe from running."""


class ProbeSkipped(Exception):
    pass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class TurnRecord:
    label: str
    session_id: str
    attempts: int
    latency_s: float
    run_state: str | None
    text: str
    stats: dict[str, Any]
    purposes: dict[str, Any]
    provider: str | None
    model: str | None
    agent: str | None
    memory_capture_state: str | None
    error: str = ""
    error_code: str = ""
    http: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProbeResult:
    probe: str
    block: str
    provider: str
    status: str
    title: str
    summary: str = ""
    latency_s: float = 0.0
    checks: list[Check] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    known_issues: list[str] = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)
    http: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return redact_obj(asdict(self))


@dataclass(frozen=True)
class ProbeDef:
    id: str
    block: str
    title: str
    fn: Callable[["ProbeContext"], str | None]
    scope: str  # "model" probes run on real providers, "local" on the fake provider
    cost_turns: int


REGISTRY: dict[str, ProbeDef] = {}


def probe(block: str, title: str, *, scope: str = "model", cost_turns: int = 1) -> Callable:
    def register(fn: Callable[["ProbeContext"], str | None]) -> Callable:
        probe_id = f"{block}.{fn.__name__}"
        REGISTRY[probe_id] = ProbeDef(probe_id, block, title, fn, scope, cost_turns)
        return fn

    return register


@dataclass
class ProbeContext:
    provider: ProviderSpec
    home: ProbeHome
    run_id: str
    out_dir: Path
    runtime_factory: Callable[[], Any]
    recorder: HttpRecorder = RECORDER
    shared: dict[str, Any] = field(default_factory=dict)
    log: Callable[[str], None] = print
    extras: dict[str, Any] = field(default_factory=dict)
    # per-probe state, reset by begin()
    checks: list[Check] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    known: list[str] = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)
    last_events: list[HttpEvent] = field(default_factory=list)
    _runtime: Any = None
    _last_turn_end: float = 0.0

    # -- lifecycle --------------------------------------------------------------
    @property
    def runtime(self) -> Any:
        if self._runtime is None:
            self._runtime = self.runtime_factory()
        return self._runtime

    def close(self) -> None:
        if self._runtime is not None:
            try:
                self._runtime.close()
            finally:
                self._runtime = None

    def begin(self) -> None:
        self.checks, self.observations, self.known, self.turns = [], [], [], []

    # -- helpers for probes ---------------------------------------------------------
    def nonce(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"

    def session(self, label: str) -> str:
        return f"mp-{self.run_id}-{self.provider.name}-{label}-{uuid.uuid4().hex[:4]}"

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, bool(ok), shorten(detail, 500)))
        return bool(ok)

    def observe(self, kind: str, **detail: Any) -> None:
        self.observations.append({"kind": kind, **redact_obj(detail)})

    def known_issue(self, ref: str) -> None:
        if ref not in self.known:
            self.known.append(ref)

    def agent(self, **kwargs: Any) -> Any:
        from openminion import Agent

        kwargs.setdefault("agent_id", self.provider.agent_id)
        return Agent(runtime=self.runtime, **kwargs)

    # -- pacing -----------------------------------------------------------------------
    def _pace(self) -> None:
        spec = self.provider
        wait = 0.0
        if spec.min_interval_s and self._last_turn_end:
            wait = max(wait, spec.min_interval_s - (time.time() - self._last_turn_end))
        if spec.pacing == "groq_headers":
            rate = self.recorder.last_rate_headers(spec.host)
            remaining = rate.get("x-ratelimit-remaining-tokens")
            if remaining is not None and remaining.isdigit() and int(remaining) < 7000:
                elapsed = time.time() - self._last_turn_end if self._last_turn_end else 0.0
                wait = max(wait, min(65.0, parse_duration(rate.get("x-ratelimit-reset-tokens", "")) + 1.0 - elapsed))
        if wait > 0:
            self.log(f"    pacing {spec.name}: sleeping {wait:.1f}s for provider rate limits")
            time.sleep(wait)

    def _rate_limit_wait(self, message: str) -> float:
        match = RETRY_IN_RE.search(message or "")
        if match:
            return min(65.0, parse_duration(match.group(1)) + 2.0)
        # Groq's TPM window is one minute and OpenMinion truncates the provider
        # message before "try again in", so wait for a full window by default
        return 62.0 if self.provider.pacing == "groq_headers" else 45.0

    # -- the turn helper ------------------------------------------------------------
    def turn(
        self,
        agent: Any,
        message: Any,
        *,
        label: str,
        session_id: str | None = None,
        max_rate_retries: int = 2,
        raw_rate_limit: bool = False,
        **run_kwargs: Any,
    ) -> tuple[Any, TurnRecord]:
        """Run one ``agent.run`` with pacing, rate-limit retries and evidence.

        Returns ``(result_or_None, record)``. Provider failures that are not
        rate limits are returned as ``(None, record)`` with ``record.error``
        set, so probes can assert on error propagation. With
        ``raw_rate_limit=True`` a rate limit is returned the same way instead
        of marking the probe BLOCKED (used by the fault-injection probes).
        """
        from openminion import ProviderError

        run_kwargs.setdefault("timeout_seconds", 180)
        attempts = 0
        while True:
            attempts += 1
            self._pace()
            mark = self.recorder.mark()
            t0 = time.time()
            result, error, code = None, "", ""
            try:
                result = agent.run(message, session_id=session_id, **run_kwargs)
            except ProviderError as exc:
                error, code = f"ProviderError {exc.code}: {exc.message}", str(exc.code)
            except Exception as exc:  # noqa: BLE001 - recorded as evidence
                error, code = f"{type(exc).__name__}: {exc}", type(exc).__name__
            self._last_turn_end = time.time()
            events = self.recorder.since(mark)
            text = getattr(result, "text", "") or ""
            text_code = ERROR_TEXT_RE.match(text)
            if result is not None and text_code:
                self.observe(
                    "provider_error_returned_as_text",
                    label=label,
                    code=text_code.group(1),
                    run_state=result.run_state,
                    text=shorten(text, 300),
                )
            limited_code = code if code in RATE_LIMIT_CODES else (text_code.group(1) if text_code and text_code.group(1) in RATE_LIMIT_CODES else "")
            if limited_code and attempts <= max_rate_retries:
                wait = self._rate_limit_wait(error or text)
                self.observe("rate_limited_retry", label=label, attempt=attempts, wait_s=round(wait, 1))
                self.log(f"    rate limited ({limited_code}); retrying in {wait:.0f}s")
                time.sleep(wait)
                continue
            self.last_events = events
            record = self._record(label, session_id, attempts, t0, result, events, error, code)
            self.turns.append(record)
            if limited_code and not raw_rate_limit:
                raise ProbeBlocked(f"provider rate limit persisted after {attempts} attempts: {shorten(error or text, 200)}")
            return result, record

    def _record(
        self,
        label: str,
        session_id: str | None,
        attempts: int,
        t0: float,
        result: Any,
        events: list[HttpEvent],
        error: str,
        code: str,
    ) -> TurnRecord:
        meta = dict(getattr(result, "metadata", {}) or {})
        return TurnRecord(
            label=label,
            session_id=str(getattr(result, "session_id", None) or session_id or ""),
            attempts=attempts,
            latency_s=round(time.time() - t0, 2),
            run_state=getattr(result, "run_state", None),
            text=shorten(getattr(result, "text", "") or "", 600),
            stats=dict(getattr(result, "stats", {}) or {}),
            purposes=_as_dict(meta.get("llm_call_counts_by_purpose")),
            provider=meta.get("provider"),
            model=meta.get("model"),
            agent=meta.get("agent") or (getattr(result, "raw", {}) or {}).get("agent_id"),
            memory_capture_state=meta.get("memory_capture_state"),
            error=shorten(error, 600),
            error_code=code,
            http=summarize(events) | {"events": [e.to_json() for e in events]},
        )

    def events_for(self, record: TurnRecord) -> list[dict[str, Any]]:
        return list(record.http.get("events", []))


def execute(defn: ProbeDef, ctx: ProbeContext) -> ProbeResult:
    ctx.begin()
    started = time.time()
    status, summary, error = PASS, "", ""
    try:
        summary = defn.fn(ctx) or ""
        if not ctx.checks:
            status, summary = ERROR, summary or "probe recorded no checks"
        elif not all(c.ok for c in ctx.checks):
            status = FAIL
    except ProbeBlocked as exc:
        status, error = BLOCKED, str(exc)
    except ProbeSkipped as exc:
        status, error = SKIP, str(exc)
    except Exception as exc:  # noqa: BLE001
        status = ERROR
        error = f"{type(exc).__name__}: {exc}\n" + "".join(traceback.format_exc(limit=6))
    all_events: list[dict[str, Any]] = [e for t in ctx.turns for e in t.http.get("events", [])]
    return ProbeResult(
        probe=defn.id,
        block=defn.block,
        provider=ctx.provider.name,
        status=status,
        title=defn.title,
        summary=shorten(summary, 600),
        latency_s=round(time.time() - started, 2),
        checks=list(ctx.checks),
        observations=list(ctx.observations),
        known_issues=list(ctx.known),
        turns=list(ctx.turns),
        http={
            "requests": len(all_events),
            "by_status": _count(all_events, "status"),
            "by_kind": _count(all_events, "kind"),
        },
        error=shorten(error, 1500),
    )


def _as_dict(value: Any) -> dict[str, Any]:
    """Run metadata values are often JSON-encoded strings; accept both."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip().startswith("{"):
        import json

        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _count(events: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for event in events:
        k = str(event.get(key))
        out[k] = out.get(k, 0) + 1
    return out
