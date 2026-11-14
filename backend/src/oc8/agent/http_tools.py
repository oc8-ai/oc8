"""Plain REST APIs as agent tools (transport "manual_http").

A connection's tools are declared in its config (`http_tools`) rather than
discovered, and every request goes to a URL a person typed in. So every
request goes through the outbound guard (`oc8.net.outbound`): the host must
resolve to a public address, or to one the operator allowed with
OC8_OUTBOUND_ALLOWED_PRIVATE; the connection is pinned to the address that
was checked (no second DNS answer at connect time); and every redirect hop
is checked again, because a public host answering with a redirect to
169.254.169.254 is exactly what the guard exists for.

What the agent gets back is capped at MAX_RESPONSE_BYTES (after decoding,
and decoded in bounded steps, so a compressed body cannot inflate past the
cap in memory) and marked when it was cut. Neither an error nor a successful
body ever carries an auth value, and a refusal never says which address a
name resolved to (that detail goes to the server log only).

Note for operators: a guarded request connects to the checked address
itself, so HTTP(S)_PROXY is not used for these calls (the same as every other
guarded fetcher in oc8).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import time
import urllib.parse
import zlib
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
#: What a tenant is told when an address is refused, whatever the reason
#: (internal address, name that does not resolve, scheme): the same text for
#: all, so a refusal cannot be used to map internal names or addresses.
BLOCKED_MESSAGE = "This address is not allowed."
_REDACTED = "[redacted]"
#: The shortest scheme-less part of a header value ("Bearer <token>") that is
#: redacted on its own; shorter parts are too likely to be ordinary words.
_MIN_SECRET_PART = 8
#: Only encodings decoded here in bounded steps are asked for and accepted.
_ACCEPT_ENCODING = "gzip, deflate"

logger = logging.getLogger(__name__)

# httpx logs every request URL at INFO ("HTTP Request: GET <url> ..."). A tool
# call's URL can carry a login in its query string, so those lines are
# dropped while a guarded call is being sent.
_SENDING_TOOL_CALL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "oc8_sending_tool_call", default=False
)


class _DropToolCallRequestLines(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _SENDING_TOOL_CALL.get()


logging.getLogger("httpx").addFilter(_DropToolCallRequestLines())

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


def secret_parts(values: Iterable[str]) -> list[str]:
    """What to redact for a set of auth values: each whole value, and for a
    value with a scheme prefix ("Bearer <token>", "Basic <b64>") the part
    after it, since an API that echoes a token echoes it bare."""
    parts: list[str] = []
    for value in values:
        if not value:
            continue
        parts.append(value)
        _scheme, sep, rest = value.partition(" ")
        rest = rest.strip()
        if sep and len(rest) >= _MIN_SECRET_PART:
            parts.append(rest)
    return parts


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


async def guarded_ip(url: str) -> str:
    """The checked address to connect to for `url`, or `OutboundBlocked`
    with BLOCKED_MESSAGE. Why it was refused (the resolved address, a name
    that does not resolve) is logged, never handed back."""
    try:
        # getaddrinfo blocks; keep it off the event loop.
        return await asyncio.to_thread(guard_outbound_url, url)
    except OutboundBlocked as exc:
        logger.info("outbound refused for a tool call: %s", exc)
        raise OutboundBlocked(BLOCKED_MESSAGE) from exc


async def _checked_ip(url: str) -> str:
    try:
        return await guarded_ip(url)
    except OutboundBlocked as exc:
        raise HttpToolError("blocked", BLOCKED_MESSAGE) from exc


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


class _Identity:
    unconsumed_tail = b""

    def decompress(self, data: bytes, max_length: int) -> bytes:
        self.unconsumed_tail = data[max_length:]
        return data[:max_length]


class _Deflate:
    """`deflate` is zlib-wrapped by the standard and raw in practice; try the
    first and fall back to the second on the first chunk."""

    def __init__(self) -> None:
        self._inner: Any = zlib.decompressobj()
        self._started = False

    @property
    def unconsumed_tail(self) -> bytes:
        return bytes(self._inner.unconsumed_tail)

    def decompress(self, data: bytes, max_length: int) -> bytes:
        if not self._started:
            self._started = True
            try:
                return bytes(self._inner.decompress(data, max_length))
            except zlib.error:
                self._inner = zlib.decompressobj(-zlib.MAX_WBITS)
        return bytes(self._inner.decompress(data, max_length))


def _decoder(content_encoding: str) -> Any:
    encodings = [e.strip().lower() for e in content_encoding.split(",") if e.strip()]
    encodings = [e for e in encodings if e != "identity"]
    if not encodings:
        return _Identity()
    if encodings == ["gzip"] or encodings == ["x-gzip"]:
        return zlib.decompressobj(zlib.MAX_WBITS | 16)
    if encodings == ["deflate"]:
        return _Deflate()
    raise HttpToolError(
        "http", f"The API answered with an unsupported content encoding ({content_encoding})."
    )


async def _read_capped(response: httpx.Response) -> tuple[str, bool]:
    """At most MAX_RESPONSE_BYTES of the decoded body. The raw bytes are
    decoded here, never more than what still fits per step (`max_length`),
    so a small compressed body cannot inflate to many MB in memory."""
    decoder = _decoder(response.headers.get("content-encoding", ""))
    buffer = bytearray()
    truncated = False
    try:
        async for chunk in response.aiter_raw():
            data = chunk
            while data:
                room = MAX_RESPONSE_BYTES - len(buffer)
                out = decoder.decompress(data, room + 1)
                if len(out) > room:
                    buffer.extend(out[:room])
                    truncated = True
                    break
                buffer.extend(out)
                data = decoder.unconsumed_tail
            if truncated:
                break
    except zlib.error as exc:
        raise HttpToolError("http", "The API answered with a body that cannot be decoded.") from exc
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

    `timeout_s` bounds each network step and the whole call: a server that
    trickles one byte at a time cannot hold a call open past it.
    """
    started = time.monotonic()
    request.headers["Accept-Encoding"] = _ACCEPT_ENCODING
    token = _SENDING_TOOL_CALL.set(True)
    try:
        async with asyncio.timeout(timeout_s):
            return await _send_hops(request, started, timeout_s, auth_headers, auth_query)
    except TimeoutError as exc:
        raise HttpToolError(
            "unreachable", f"The API did not answer within {timeout_s:g} seconds."
        ) from exc
    finally:
        _SENDING_TOOL_CALL.reset(token)


async def _send_hops(
    request: httpx.Request,
    started: float,
    timeout_s: float,
    auth_headers: frozenset[str],
    auth_query: frozenset[str],
) -> HttpResponse:
    # One client per hop: each hop is pinned to its own checked address.
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
        secrets: Iterable[str] = (),
    ) -> None:
        """`secrets` are raw secret values a login put into a request in a
        form no header shows verbatim (a password inside a Basic credential,
        a fetched access token); they are redacted like the header values."""
        self._base_url = base_url.rstrip("/")
        self._extra_secrets = [str(v) for v in secrets if v]
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
        return [*secret_parts(self._headers.values()), *self._extra_secrets]

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
        secrets = self._secrets()
        if response.status >= 400:
            # Redacted whole, then cut: a cut first could split a secret so
            # that the part left no longer matches it.
            excerpt = redact(response.text, secrets)[:ERROR_EXCERPT_CHARS]
            kind: ErrorClass = "auth" if response.status in (401, 403) else "http"
            raise HttpToolError(
                kind, f"{response.status}: {excerpt}", status=response.status, excerpt=excerpt
            )
        # An API that echoes its credential (a debug or whoami endpoint) must
        # not hand it to the model either.
        text = redact(response.text, secrets) or "(no output)"
        return text + TRUNCATION_MARKER if response.truncated else text
