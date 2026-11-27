"""One-way credential redaction.

Originally written for the raw-completion Copilot chat path (retired in
Task 5 of the unified-assistant plan). `carries_secret_value` is the tenant
Assistant's pre-model secret gate: chat send (`oc8.chat.service`), a queued
turn (`oc8.chat.carry_over`), a steering input (`oc8.runtime.run_inputs`) and
a clarification answer (`oc8.runtime.clarification`) -- every door checks what
the PERSON wrote. `redact_text`/`redact_value` are kept as
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
#: The same value written as a sentence ("my password is hunter2", "mein Token
#: ist abc123"). The value must hold a digit, so "the password is required"
#: stays a sentence about a password.
_PROSE_VALUE = re.compile(
    r"(?i)\b(secret|password|credential|token|api[_-]?key)\s+(is|ist|lautet)\s+"
    r"(?=[^\s,;]*\d)[^\s,;]+"
)


def redact_text(value: str) -> str:
    """Remove common credential-bearing text forms without retaining the value."""
    value = _ASSIGNMENT.sub(r"\1=[redacted]", value)
    value = _PROSE_VALUE.sub(r"\1 \2 [redacted]", value)
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
    """A credential VALUE in the text (`password=...`, `token: ...`,
    `Bearer ...`), not just a word about one. The tenant Assistant never
    accepts or forwards a credential value, so this is its gate on what a
    person writes; a word alone is ordinary content -- "How many tokens did I
    use?" is the Cost Center's question, "reset my password" a request."""
    return bool(_ASSIGNMENT.search(text) or _PROSE_VALUE.search(text) or _BEARER.search(text))
