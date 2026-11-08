"""Turn a validator's error into what a person can fix (connections design:
"Verbindung prüfen" shows a human sentence, not a stack trace)."""

from __future__ import annotations

_AUTH = (
    "401",
    "403",
    "unauthor",
    "forbidden",
    "access denied",
    "authentication",
    "wrong login",
    "password",
    "invalid api key",
    "invalid token",
    "credentials",
)
_UNREACHABLE = (
    "connecterror",
    "connection refused",
    "name or service",
    "nodename",
    "servname",
    "timed out",
    "timeout",
    "unreachable",
    "getaddrinfo",
    "no route",
    "ssl",
)


def classify_check_error(message: str) -> str:
    low = message.lower()
    if any(needle in low for needle in _AUTH):
        return "auth"
    if any(needle in low for needle in _UNREACHABLE):
        return "unreachable"
    return "invalid"
