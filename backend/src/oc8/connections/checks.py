"""Turn a validator's error into what a person can fix (connections design:
"Verbindung prüfen" shows a human sentence, not a stack trace)."""

from __future__ import annotations

import re

# Whole words only: "14013" must not read as an HTTP 401.
_AUTH = re.compile(
    r"\b(?:401|403|unauthori[sz]ed|forbidden|access denied|authentication|wrong login"
    r"|password|invalid api key|invalid token|credentials)\b",
    re.IGNORECASE,
)
_UNREACHABLE = re.compile(
    r"\b(?:connecterror|connection refused|name or service|nodename|servname|timed out"
    r"|timeout|unreachable|getaddrinfo|no route|ssl)\b",
    re.IGNORECASE,
)


def classify_check_error(message: str) -> str:
    if _AUTH.search(message):
        return "auth"
    if _UNREACHABLE.search(message):
        return "unreachable"
    return "invalid"
