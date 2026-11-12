"""Moodle web service REST client for the bridge process.

Runs inside the stdio bridge (`python -m mcp_bridge`), a separate process
with no database and no secret store: the core resolves the connection's
credential before launch and passes it in as MOODLE_URL / MOODLE_TOKEN.

Every call is one POST to `/webservice/rest/server.php` carrying the token,
the function name and the function's parameters as form fields. The token
travels in the body only, so it never lands in a URL, an access log or an
exception that echoes one.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

URL_ENV = "MOODLE_URL"
TOKEN_ENV = "MOODLE_TOKEN"
TIMEOUT = 20.0
_REST_PATH = "/webservice/rest/server.php"
#: Plain http is refused except on hosts that only exist on a developer's
#: machine: a token sent in clear text to a real site is a leaked token.
_PLAIN_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "host.docker.internal"})

#: Swapped for an `httpx.MockTransport` by the tests; None means the real network.
transport: httpx.AsyncBaseTransport | None = None


class MoodleError(RuntimeError):
    """A failure worded for the agent and the operator. `__main__` turns it
    into an `is_error=True` tool result, so this text is what they read."""


def encode(params: Mapping[str, Any]) -> dict[str, str]:
    """Flatten nested parameters into Moodle's form-field names:
    `{"users": [{"username": "ana"}]}` becomes `users[0][username]=ana`."""
    out: dict[str, str] = {}
    for key, value in params.items():
        _encode_value(str(key), value, out)
    return out


def _encode_value(name: str, value: Any, out: dict[str, str]) -> None:
    if value is None:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _encode_value(f"{name}[{key}]", item, out)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _encode_value(f"{name}[{index}]", item, out)
    elif isinstance(value, bool):
        out[name] = "1" if value else "0"
    else:
        out[name] = str(value)


def site_url() -> str:
    raw = os.environ.get(URL_ENV, "").strip().rstrip("/")
    if not raw:
        raise MoodleError("MOODLE_URL is not set. Complete the Moodle connection setup in oc8.")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise MoodleError(f"MOODLE_URL is not a valid site address: {raw}")
    if parts.scheme == "http" and parts.hostname not in _PLAIN_HTTP_HOSTS:
        raise MoodleError(f"Moodle must be reached over https, not {raw}.")
    return raw


def _token() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise MoodleError("MOODLE_TOKEN is not set. Complete the Moodle connection setup in oc8.")
    return token


def redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


async def call(function: str, params: Mapping[str, Any] | None = None) -> Any:
    """Call one Moodle web service function and return its decoded answer."""
    url = site_url()
    token = _token()
    data = {
        "wstoken": token,
        "wsfunction": function,
        "moodlewsrestformat": "json",
        **encode(params or {}),
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as http:
            response = await http.post(f"{url}{_REST_PATH}", data=data)
    except httpx.TimeoutException:
        raise MoodleError(f"Moodle at {url} is not reachable (timed out after 20 s).") from None
    except httpx.HTTPError as exc:
        reason = redact(f"{type(exc).__name__}: {exc}", token)
        raise MoodleError(f"Moodle at {url} is not reachable ({reason}).") from None
    if response.status_code != 200:
        raise MoodleError(f"Moodle at {url} is not reachable (HTTP {response.status_code}).")
    try:
        body = response.json()
    except ValueError:
        raise MoodleError(
            f"Moodle at {url} did not answer with JSON. Check the site address and that "
            "the REST protocol is enabled (Site administration → Server → Web services → "
            "Manage protocols)."
        ) from None
    if isinstance(body, dict) and "exception" in body:
        raise MoodleError(redact(_explain(function, body), token))
    return body


def _explain(function: str, body: Mapping[str, Any]) -> str:
    code = str(body.get("errorcode") or "")
    exception = str(body.get("exception") or "")
    message = str(body.get("message") or "").strip()
    if code == "invalidtoken":
        return (
            "The Moodle token is invalid or expired. Create a new token for the oc8 service user."
        )
    if code in ("accessexception", "servicenotavailable") or exception.endswith(
        "webservice_access_exception"
    ):
        return (
            f"The external service does not allow {function}. Add it to the oc8 service in "
            "Moodle (Site administration → Server → Web services → External services)."
        )
    if code in ("nopermissions", "requireloginerror") or exception.endswith(
        ("required_capability_exception", "require_login_exception")
    ):
        return f"The Moodle service user lacks permission for this: {message}"
    return f"Moodle rejected the request ({code or exception}): {message}"
