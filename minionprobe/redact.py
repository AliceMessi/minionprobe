"""Redaction helpers. Everything MinionProbe writes to disk goes through here.

Provider error bodies can echo account identifiers (Groq includes the
organization id in rate-limit messages) and, in the worst case, credentials.
We scrub them before anything reaches results/ or issue drafts.
"""

from __future__ import annotations

import os
import re
from typing import Any

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"org_[A-Za-z0-9]{8,}"), "org_REDACTED"),
    (re.compile(r"gsk_[A-Za-z0-9]{16,}"), "<GROQ_KEY_REDACTED>"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "<GOOGLE_KEY_REDACTED>"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "<API_KEY_REDACTED>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1<TOKEN_REDACTED>"),
    (re.compile(r"(?i)(x-goog-api-key|api[_-]?key)(\"?\s*[:=]\s*\"?)[A-Za-z0-9._\-]{12,}"), r"\1\2<REDACTED>"),
]

_SECRET_ENV_VARS = (
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "CORTENSOR_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
)


def _env_secrets() -> list[str]:
    values = []
    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name, "")
        if len(value) >= 8:
            values.append(value)
    return values


def redact(text: str) -> str:
    """Remove known secret shapes, literal key values and account ids from text."""
    if not text:
        return text
    for secret in _env_secrets():
        text = text.replace(secret, "<SECRET_REDACTED>")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_obj(value: Any) -> Any:
    """Recursively redact strings inside JSON-like data."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    return value


def shorten(text: str, limit: int = 400) -> str:
    text = redact(str(text or ""))
    return text if len(text) <= limit else text[: limit - 3] + "..."
