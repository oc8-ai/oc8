"""Turn a validator's error into what a person can fix (connections design:
"Verbindung prüfen" shows a human sentence, not a stack trace)."""

from __future__ import annotations

import re
import smtplib
import ssl

import httpx

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


def reason_for_exception(exc: BaseException) -> str:
    """The class of failure from the exception's type chain, falling back to
    the wording only when the types say nothing."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, httpx.HTTPStatusError):
            if cur.response.status_code in (401, 403):
                return "auth"
        elif isinstance(cur, smtplib.SMTPAuthenticationError):
            return "auth"
        elif isinstance(cur, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)):
            return "unreachable"
        elif isinstance(cur, (smtplib.SMTPException, httpx.UnsupportedProtocol)):
            # The server answered (or the address names no usable protocol):
            # SMTPException is an OSError and UnsupportedProtocol a
            # TransportError, but neither means the target is unreachable.
            return "invalid"
        elif isinstance(cur, (httpx.TransportError, ssl.SSLError, OSError, TimeoutError)):
            return "unreachable"
        cur = cur.__cause__ or cur.__context__
    return classify_check_error(str(exc))
