"""One-way credential redaction.

Originally written for the raw-completion Copilot chat path (retired in
Task 5 of the unified-assistant plan); `is_secret_request` is now the
tenant Assistant's pre-model secret gate in `oc8.chat.service.send_message`,
its one remaining caller. `redact_text`/`redact_value` are kept as
general-purpose helpers for any future caller that needs to strip
credential-shaped values before they reach a model or a durable store.
"""

from __future__ import annotations

import re
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(?:secret|password|credential|token|api[_-]?key|authorization)", re.IGNORECASE
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(secret|password|credential|token|api[_-]?key)\b\s*[:=]\s*[^\s,;]+"
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")


def redact_text(value: str) -> str:
    """Remove common credential-bearing text forms without retaining the value."""
    value = _ASSIGNMENT.sub(r"\1=[redacted]", value)
    return _BEARER.sub("Bearer [redacted]", value)


def redact_value(value: Any) -> Any:
    """Recursively remove values under sensitive keys before serialization."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[redacted]" if _SENSITIVE_KEY.search(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    return value


def carries_secret_value(text: str) -> bool:
    """A credential VALUE in the text (`password=...`, `Bearer ...`), not just a
    word about one: for stored configuration such as project instructions,
    where "explain our password policy" is ordinary content."""
    return bool(_ASSIGNMENT.search(text) or _BEARER.search(text))


def is_secret_request(message: str) -> bool:
    """The tenant Assistant never solicits, accepts, or forwards credential values."""
    return bool(_SENSITIVE_KEY.search(message))
