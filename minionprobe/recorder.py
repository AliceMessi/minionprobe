"""HTTP evidence recorder.

OpenMinion talks to model providers through httpx. MinionProbe wraps
``httpx.Client.send`` (and the async variant) so each probe can see what
actually went over the wire: host, model, which tools were offered, the
tool_choice, the HTTP status, a redacted error body and the provider's
rate-limit headers. Request headers (and so the Authorization header) are
never read.

This is observation only: the request and response objects are passed through
unchanged.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from .redact import redact, shorten

_RATE_HEADERS = (
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "retry-after",
)


@dataclass
class HttpEvent:
    seq: int
    ts: float
    status: int
    host: str
    path: str
    model: str | None
    tools: list[str]
    tool_choice: Any
    n_messages: int
    has_tool_result: bool
    kind: str
    error: str = ""
    rate: dict[str, str] = field(default_factory=dict)
    duration_ms: int = 0
    # Kept in memory only (never serialized): used by probes that need to
    # assert on request content such as tool results reaching the model.
    _messages: list[dict[str, Any]] | None = field(default=None, repr=False)
    _response_text: str = field(default="", repr=False)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("_messages", None)
        data.pop("_response_text", None)
        return data


def classify_call(tools: list[str], tool_choice: Any) -> str:
    """Name the kind of LLM call from what the runtime sent.

    OpenMinion's hidden control calls (memory reflect, freshness classifier,
    closure judge) are forced ``submit_output`` calls. User-visible agent loop
    calls offer the agent's own tools with ``tool_choice=auto`` (or no tools).
    """
    forced = ""
    if isinstance(tool_choice, dict):
        forced = str(tool_choice.get("function", {}).get("name", ""))
    if forced == "submit_output" or tools == ["submit_output"]:
        return "control"
    return "agent_loop"


class HttpRecorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[HttpEvent] = []
        self._installed = False
        self._orig_send = None
        self._orig_async_send = None

    # -- installation -----------------------------------------------------
    def install(self) -> "HttpRecorder":
        if self._installed:
            return self
        import httpx

        recorder = self
        orig_send = httpx.Client.send
        orig_async_send = httpx.AsyncClient.send
        self._orig_send, self._orig_async_send = orig_send, orig_async_send

        def send(client: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
            started = time.time()
            response = orig_send(client, request, *args, **kwargs)
            recorder._record(request, response, started)
            return response

        async def async_send(client: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
            started = time.time()
            response = await orig_async_send(client, request, *args, **kwargs)
            recorder._record(request, response, started, is_async=True)
            return response

        httpx.Client.send = send  # type: ignore[method-assign]
        httpx.AsyncClient.send = async_send  # type: ignore[method-assign]
        self._installed = True
        return self

    def uninstall(self) -> None:
        if not self._installed:
            return
        import httpx

        httpx.Client.send = self._orig_send  # type: ignore[method-assign]
        httpx.AsyncClient.send = self._orig_async_send  # type: ignore[method-assign]
        self._installed = False

    # -- recording ----------------------------------------------------------
    def _record(self, request: Any, response: Any, started: float, is_async: bool = False) -> None:
        url = urlparse(str(request.url))
        if not url.path.endswith("/chat/completions"):
            return
        try:
            body = json.loads(request.content or b"{}")
        except Exception:  # noqa: BLE001 - streaming or non-JSON bodies
            body = {}
        tools = [
            str((t.get("function") or {}).get("name", ""))
            for t in (body.get("tools") or [])
            if isinstance(t, dict)
        ]
        messages = body.get("messages") or []
        error = ""
        response_text = ""
        if not is_async:
            try:
                response.read()
                response_text = response.text
            except Exception:  # noqa: BLE001 - streamed responses
                response_text = ""
        if response.status_code >= 400:
            error = shorten(response_text, 900)
        rate = {
            name: response.headers.get(name, "")
            for name in _RATE_HEADERS
            if response.headers.get(name)
        }
        with self._lock:
            event = HttpEvent(
                seq=len(self.events),
                ts=started,
                status=int(response.status_code),
                host=url.hostname or "",
                path=url.path,
                model=body.get("model"),
                tools=tools,
                tool_choice=body.get("tool_choice"),
                n_messages=len(messages),
                has_tool_result=any(isinstance(m, dict) and m.get("role") == "tool" for m in messages),
                kind=classify_call(tools, body.get("tool_choice")),
                error=error,
                rate=rate,
                duration_ms=int((time.time() - started) * 1000),
                _messages=messages,
                _response_text=redact(response_text[:6000]),
            )
            self.events.append(event)

    # -- queries --------------------------------------------------------------
    def mark(self) -> int:
        with self._lock:
            return len(self.events)

    def since(self, mark: int) -> list[HttpEvent]:
        with self._lock:
            return list(self.events[mark:])

    def last_rate_headers(self, host: str) -> dict[str, str]:
        with self._lock:
            for event in reversed(self.events):
                if event.host == host and event.rate:
                    return dict(event.rate)
        return {}


_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def parse_duration(text: str) -> float:
    """Parse Groq-style durations such as ``40.6s``, ``577ms`` or ``1m26.4s``."""
    text = (text or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    total = 0.0
    for number, unit in _DURATION_PART.findall(text):
        value = float(number)
        total += {"ms": value / 1000, "s": value, "m": value * 60, "h": value * 3600}[unit]
    return total


def summarize(events: list[HttpEvent]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for event in events:
        by_status[str(event.status)] = by_status.get(str(event.status), 0) + 1
        by_kind[event.kind] = by_kind.get(event.kind, 0) + 1
    return {
        "requests": len(events),
        "by_status": by_status,
        "by_kind": by_kind,
        "hosts": sorted({e.host for e in events}),
        "models": sorted({str(e.model) for e in events if e.model}),
        "errors": [
            {"status": e.status, "kind": e.kind, "error": shorten(e.error, 300)}
            for e in events
            if e.status >= 400
        ][:6],
    }


RECORDER = HttpRecorder()
