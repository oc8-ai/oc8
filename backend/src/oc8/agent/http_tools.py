"""Plain REST APIs as agent tools (transport "manual_http").

A connection's tools are declared in its config (`http_tools`) rather than
discovered, and every request goes to a URL a person typed in. So every
request goes through the outbound guard (`oc8.net.outbound`): the host must
resolve to a public address, or to one the operator allowed with
OC8_OUTBOUND_ALLOWED_PRIVATE; the connection is pinned to the address that
was checked (no second DNS answer at connect time); and every redirect hop
is checked again, because a public host answering with a redirect to
169.254.169.254 is exactly what the guard exists for.

What the agent gets back is capped at MAX_RESPONSE_BYTES and marked when it
was cut, and no error text ever carries an auth value.
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal

import httpx

from oc8.modelrouter.types import NeutralTool
from oc8.net.outbound import USER_AGENT, OutboundBlocked, PinnedTransport, guard_outbound_url

#: The same allowance as `mcp_client.MCP_REQUEST_TIMEOUT_SECONDS` (not
#: imported from there: `mcp_client` imports this module).
HTTP_TOOL_TIMEOUT_SECONDS = 180.0
#: The most of one response body an agent is handed.
MAX_RESPONSE_BYTES = 1_000_000
TRUNCATION_MARKER = "\n[truncated: the response was larger than 1 MB]"
MAX_REDIRECTS = 3
#: How much of an error body goes back to the agent.
ERROR_EXCERPT_CHARS = 500
_REDACTED = "[redacted]"

ErrorClass = Literal["auth", "token", "blocked", "unreachable", "http"]


class HttpToolError(RuntimeError):
    """A call that failed for a reason a person can act on. A RuntimeError,
    so every caller that already turns a raising tool into "ERROR: ..." for
    the model keeps doing so."""

    def __init__(
        self, kind: ErrorClass, message: str, *, status: int | None = None, excerpt: str = ""
    ) -> None:
        super().__init__(message)
        self.kind: ErrorClass = kind
        self.status = status
        self.excerpt = excerpt


@dataclass(frozen=True)
class HttpResponse:
    status: int
    text: str
    truncated: bool
    duration_ms: int


def redact(text: str, secrets: Iterable[str]) -> str:
    """`text` with every secret replaced, also in its URL-encoded forms (a
    query-string key comes back from an API that echoes its request URL)."""
    for secret in secrets:
        if not secret:
            continue
        forms = {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}
        for form in sorted(forms, key=len, reverse=True):
            text = text.replace(form, _REDACTED)
    return text


async def _checked_ip(url: str) -> str:
    try:
        # getaddrinfo blocks; keep it off the event loop.
        return await asyncio.to_thread(guard_outbound_url, url)
    except OutboundBlocked as exc:
        raise HttpToolError("blocked", f"This address is not allowed ({exc}).") from exc


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return (url.scheme, url.host, url.port)


def _without_auth(
    request: httpx.Request, auth_headers: frozenset[str], auth_query: frozenset[str]
) -> httpx.Request:
    """A redirect to another origin never takes the login along. httpx drops
    `Authorization` itself; a custom API-key header or query key it cannot
    know about, so they are removed here."""
    url = request.url
    for name in auth_query:
        url = url.copy_remove_param(name)
    headers = httpx.Headers(
        [(k, v) for k, v in request.headers.multi_items() if k.lower() not in auth_headers]
    )
    return httpx.Request(
        request.method, url, headers=headers, stream=request.stream, extensions=request.extensions
    )


async def _read_capped(response: httpx.Response) -> tuple[str, bool]:
    buffer = bytearray()
    truncated = False
    async for chunk in response.aiter_bytes():
        room = MAX_RESPONSE_BYTES - len(buffer)
        if len(chunk) > room:
            buffer.extend(chunk[:room])
            truncated = True
            break
        buffer.extend(chunk)
    return bytes(buffer).decode(response.encoding or "utf-8", errors="replace"), truncated


async def send_guarded(
    request: httpx.Request,
    *,
    timeout_s: float,
    auth_headers: frozenset[str] = frozenset(),
    auth_query: frozenset[str] = frozenset(),
) -> HttpResponse:
    """Send `request` the guarded way and read at most MAX_RESPONSE_BYTES.

    Redirects are followed by hand (at most MAX_REDIRECTS), each hop checked
    and pinned on its own. Any HTTP status is returned, not raised: the caller
    decides what an error is. Raises HttpToolError("blocked"|"unreachable"|
    "http") for a refused address, a network failure or a redirect loop.
    `auth_headers` (lower-case) and `auth_query` are dropped on a redirect to
    another origin.
    """
    started = time.monotonic()
    origin = _origin(request.url)
    current = request
    for _hop in range(MAX_REDIRECTS + 1):
        ip = await _checked_ip(str(current.url))
        # A request not built by a client carries no timeout of its own, and
        # the transport then waits for ever: set it on every hop.
        current.extensions["timeout"] = httpx.Timeout(timeout_s).as_dict()
        try:
            async with httpx.AsyncClient(
                timeout=timeout_s, follow_redirects=False, transport=PinnedTransport(ip)
            ) as client:
                response = await client.send(current, stream=True)
                try:
                    nxt = response.next_request
                    if response.is_redirect and response.has_redirect_location and nxt is not None:
                        if _origin(nxt.url) != origin:
                            nxt = _without_auth(nxt, auth_headers, auth_query)
                        current = nxt
                        continue
                    text, truncated = await _read_capped(response)
                    return HttpResponse(
                        status=response.status_code,
                        text=text,
                        truncated=truncated,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                finally:
                    await response.aclose()
        except httpx.TransportError as exc:
            raise HttpToolError(
                "unreachable", f"The API is not reachable ({type(exc).__name__})."
            ) from exc
    raise HttpToolError("http", f"The API redirected more than {MAX_REDIRECTS} times.")


class HttpToolSession:
    """A drop-in substitute for `McpSession` over a plain REST API with no
    MCP support at all: tools are declared in the connection's config
    (`transport="manual_http"`) rather than discovered via a handshake, so
    `__aenter__` performs no network I/O."""

    def __init__(
        self,
        base_url: str,
        http_tools: list[dict[str, Any]],
        *,
        headers: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http_tools = {str(t["name"]): t for t in http_tools}
        self._headers = dict(headers or {})
        self._timeout_s = HTTP_TOOL_TIMEOUT_SECONDS if timeout_s is None else timeout_s
        self._started = False
        self.tools: list[NeutralTool] = []

    async def __aenter__(self) -> HttpToolSession:
        self._started = True
        self.tools = [
            NeutralTool(
                name=str(t["name"]),
                description=str(t.get("description", "")),
                parameters=t.get("param_schema") or {"type": "object", "properties": {}},
            )
            for t in self._http_tools.values()
        ]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._started = False

    def _secrets(self) -> list[str]:
        return [value for value in self._headers.values() if value]

    def _auth_header_names(self) -> frozenset[str]:
        return frozenset(name.lower() for name in self._headers)

    def _build(self, tool: dict[str, Any], arguments: dict[str, Any]) -> httpx.Request:
        url_template = str(tool.get("url_template", ""))
        placeholders = set(re.findall(r"\{(\w+)\}", url_template))
        try:
            # Quoted so a model-supplied value (e.g. containing `../`, `?` or
            # `#`) can't escape the path segment it was meant to fill.
            path = url_template.format(
                **{k: urllib.parse.quote(str(arguments[k]), safe="") for k in placeholders}
            )
        except KeyError as exc:
            raise RuntimeError(f"missing required parameter {exc}") from exc
        remaining = {k: v for k, v in arguments.items() if k not in placeholders}
        method = str(tool.get("method", "GET")).upper()
        headers = {"User-Agent": USER_AGENT, **self._headers}
        if method in ("GET", "DELETE", "HEAD"):
            return httpx.Request(method, self._base_url + path, headers=headers, params=remaining)
        return httpx.Request(method, self._base_url + path, headers=headers, json=remaining)

    async def request(self, name: str, arguments: dict[str, Any]) -> HttpResponse:
        """One call, any status. Raises HttpToolError for a refused address,
        an unreachable API or a redirect loop."""
        if not self._started:
            raise RuntimeError("HTTP tool session not started")
        tool = self._http_tools.get(name)
        if tool is None:
            raise RuntimeError(f"unknown tool {name!r}")
        return await send_guarded(
            self._build(tool, arguments),
            timeout_s=self._timeout_s,
            auth_headers=self._auth_header_names(),
        )

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        response = await self.request(name, arguments)
        if response.status >= 400:
            excerpt = redact(response.text[:ERROR_EXCERPT_CHARS], self._secrets())
            kind: ErrorClass = "auth" if response.status in (401, 403) else "http"
            raise HttpToolError(
                kind, f"{response.status}: {excerpt}", status=response.status, excerpt=excerpt
            )
        text = response.text or "(no output)"
        return text + TRUNCATION_MARKER if response.truncated else text
