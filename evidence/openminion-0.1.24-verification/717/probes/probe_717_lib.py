"""Helpers for probe_717.py (installed package only, no repo test fixtures).

- RateLimitStub: an OpenAI-compatible chat-completions server on 127.0.0.1
  (ThreadingHTTPServer, no external network). Each case scripts, per model,
  a queue of replies: `("429", retry_after_or_None)` answers HTTP 429 with an
  OpenAI-style error body, an `X-Request-ID` header and, when given, a
  `Retry-After` header; `("ok", text)` answers HTTP 200 with a chat completion
  and fixed usage (11 prompt / 5 completion / 16 total tokens). When a model's
  queue is empty, or for a model with no queue, the reply is the #420/#745
  harness's normal answer (`probe_420_lib.ScriptedProvider.respond`, byte copy),
  so a full runtime turn gets its classifier/decide/judge answers. Every request
  is recorded with its model, kind, HTTP status and server-side
  `time.monotonic()` (same process as the client, so gaps are comparable).
- `rate_limit_first(kind, n, retry_after)`: answer the first `n` requests of a
  harness kind (for example `decide`) with 429, for the full-runtime cases.
- A hint written `date+N` is sent as an HTTP-date computed when the 429 is
  served: the next whole second at least N s later (so N to N+1 s ahead). The
  log keeps the header value actually sent and that date as epoch seconds.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from probe_420_lib import ScriptedProvider

USAGE = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
ERROR_BODY = {
    "error": {
        "message": "Rate limit reached for probe-717 requests per minute.",
        "type": "requests",
        "code": "rate_limit_exceeded",
    }
}


def completion(text: str, model: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-probe-717",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": dict(USAGE),
    }


class RateLimitStub:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []  # used by ScriptedProvider.respond
        self.log: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.scripts: dict[str, deque] = {}
        self.kind_limits: dict[str, list[Any]] = {}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                received = time.monotonic()
                wall = time.time()
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status, headers, payload, kind = stub.reply(body, self.path)
                with stub.lock:
                    stub.log.append({"index": len(stub.log), "model": body.get("model"), "kind": kind,
                                     "status": status, "t": received, "wall": wall,
                                     "retry_after": headers.get("Retry-After"),
                                     "hint_epoch": float(headers["X-Probe-Hint-Epoch"])
                                     if "X-Probe-Hint-Epoch" in headers else None})
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def reset(self, scripts: dict[str, list[tuple[str, Any]]] | None = None) -> None:
        with self.lock:
            self.log = []
            self.requests = []
            self.scripts = {model: deque(items) for model, items in (scripts or {}).items()}
            self.kind_limits = {}

    def rate_limit_first(self, kind: str, n: int, retry_after: str | None) -> None:
        with self.lock:
            self.kind_limits[kind] = [n, retry_after]

    def _limited(self, retry_after: str | None) -> tuple[int, dict[str, str], dict[str, Any]]:
        headers = {"X-Request-ID": f"req-probe-717-{len(self.log)}"}
        if retry_after is not None and retry_after.startswith("date+"):
            target = int(time.time()) + int(retry_after[5:]) + 1
            headers["Retry-After"] = format_datetime(datetime.fromtimestamp(target, tz=timezone.utc), usegmt=True)
            headers["X-Probe-Hint-Epoch"] = str(target)
        elif retry_after is not None:
            headers["Retry-After"] = retry_after
        return 429, headers, ERROR_BODY

    def reply(self, body: dict[str, Any], path: str) -> tuple[int, dict[str, str], dict[str, Any], str]:
        model = str(body.get("model") or "")
        with self.lock:
            queue = self.scripts.get(model)
            item = queue.popleft() if queue else None
        if item is not None:
            kind, value = item
            if kind == "429":
                return (*self._limited(value), "scripted")
            return 200, {}, completion(str(value), model), "scripted"
        normal = ScriptedProvider.respond(self, body, path)  # type: ignore[arg-type]
        with self.lock:
            kind = self.requests[-1]["kind"] if self.requests else "other_text"
            limit = self.kind_limits.get(kind)
            if limit is not None and limit[0] > 0:
                limit[0] -= 1
                return (*self._limited(limit[1]), kind)
        return 200, {}, normal, kind

    def entries(self, model: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(e) for e in self.log if model is None or e["model"] == model]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
