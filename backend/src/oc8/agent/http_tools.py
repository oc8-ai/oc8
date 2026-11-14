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
import base64
import contextlib
import contextvars
import hashlib
import json
import logging
import math
import re
import time
import urllib.parse
import weakref
import zlib
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, field
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


class _HideQueryStrings(logging.Filter):
    """httpx2 (the client under the MCP SDK, used for remote MCP servers)
    logs each request URL at INFO too. No HTTP tool login travels through it,
    but a remote server URL can carry a key in its query string, so the query
    of any URL it logs is hidden."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(_without_query(arg) for arg in record.args)
        return True


def _without_query(arg: Any) -> Any:
    if not (hasattr(arg, "query") and hasattr(arg, "copy_with")):
        return arg
    text = str(arg)
    base, sep, _query = text.partition("?")
    return f"{base}?{_REDACTED}" if sep else text


logging.getLogger("httpx2").addFilter(_HideQueryStrings())

ParamLocation = Literal["path", "query", "header", "body"]
_QUERY_METHODS = frozenset({"GET", "DELETE", "HEAD"})
#: Never settable from a tool argument: transport and credential headers.
_RESERVED_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "user-agent",
    }
)
_SCALARS = (str, int, float, bool)
_NO_BODY = object()


@dataclass(frozen=True)
class PlacedRequest:
    method: str
    path: str
    query: dict[str, Any]
    headers: dict[str, str]
    body: Any
    has_body: bool


_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _text(value: Any) -> str:
    """One text form of a value for a path segment, a header or a structured
    query value: booleans as true/false, structures as JSON."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value)


def _path_segment(value: Any) -> str:
    # Quoted so a model-supplied value (e.g. containing `/`, `?` or `#`) can't
    # escape the segment it fills. Dots are unreserved, so a bare `.` or `..`
    # is encoded by hand: the URL layer would otherwise resolve it away.
    quoted = urllib.parse.quote(_text(value), safe="")
    return quoted.replace(".", "%2E") if set(quoted) == {"."} else quoted


def _query_value(value: Any) -> Any:
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, list) and all(isinstance(v, _SCALARS) for v in value):
        return value
    return _text(value)


def _header_pair(name: str, value: Any) -> tuple[str, str]:
    text = _text(value)
    if not _HEADER_NAME.match(name):
        raise HttpToolError("http", f"Invalid header name {name!r}.")
    if any(c in text for c in "\r\n\x00"):
        raise HttpToolError("http", f"Invalid value for header {name!r}.")
    return name, text


def place_arguments(
    tool: dict[str, Any], arguments: dict[str, Any], *, protected_headers: frozenset[str]
) -> PlacedRequest:
    """Where each argument of one call goes.

    URL-template placeholders always fill the path. Every other argument goes
    where `param_locations` says; one it does not list (or every one, when the
    tool has no `param_locations` -- every tool made before this existed)
    follows the old rule: GET/DELETE/HEAD send it in the query string, every
    other method in the JSON body. `body_param` names one argument that IS the
    whole body (an array, or an object other body fields merge into). A header
    argument never replaces a credential or transport header.
    """
    method = str(tool.get("method", "GET")).upper()
    template = str(tool.get("url_template", ""))
    placeholders = set(_PLACEHOLDER.findall(template))

    def _fill(match: re.Match[str]) -> str:
        key = match.group(1)
        if arguments.get(key) is None:
            raise RuntimeError(f"missing required parameter {key!r}")
        return _path_segment(arguments[key])

    path = _PLACEHOLDER.sub(_fill, template)
    raw_locations = tool.get("param_locations")
    locations: dict[str, Any] = raw_locations if isinstance(raw_locations, dict) else {}
    raw_body_param = tool.get("body_param")
    body_param = raw_body_param if isinstance(raw_body_param, str) and raw_body_param else None
    fallback: ParamLocation = "query" if method in _QUERY_METHODS else "body"
    blocked_headers = protected_headers | _RESERVED_HEADERS

    query: dict[str, Any] = {}
    headers: dict[str, str] = {}
    body_fields: dict[str, Any] = {}
    whole_body: Any = _NO_BODY
    for key, value in arguments.items():
        if key in placeholders:
            continue
        if key == body_param:
            whole_body = value
            continue
        where = locations.get(key, fallback)
        if where == "query":
            if value is not None:
                query[key] = _query_value(value)
        elif where == "header":
            if key.lower() not in blocked_headers and value is not None:
                name, text = _header_pair(key, value)
                if not text.isascii():
                    # httpx sends header values as ASCII; its codec error
                    # would carry the whole value and no error class.
                    raise HttpToolError("http", f"Header {name!r} can only carry ASCII text.")
                headers[name] = text
        elif where == "body":
            body_fields[key] = value
        # "path" for a name the template does not contain has nowhere to go.

    if whole_body is not _NO_BODY:
        body = (
            {**whole_body, **body_fields}
            if isinstance(whole_body, dict) and body_fields
            else whole_body
        )
        return PlacedRequest(method, path, query, headers, body, True)
    if method in _QUERY_METHODS and not body_fields:
        return PlacedRequest(method, path, query, headers, None, False)
    return PlacedRequest(method, path, query, headers, body_fields, method != "HEAD")


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


#: How much of an answer a person sees in a test call.
TEST_EXCERPT_CHARS = 2000


@dataclass(frozen=True)
class CallOutcome:
    """One test call, as the person setting a connection up sees it."""

    ok: bool
    status: int | None
    duration_ms: int
    excerpt: str
    truncated: bool
    error_class: ErrorClass | None
    message: str


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


def _with_query(request: httpx.Request, params: Mapping[str, str]) -> httpx.Request:
    url = request.url
    for name, value in params.items():
        url = url.copy_set_param(name, value)
    return httpx.Request(
        request.method,
        url,
        headers=request.headers,
        stream=request.stream,
        extensions=request.extensions,
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
    login_query: Mapping[str, str] | None = None,
    same_origin_only: bool = False,
) -> HttpResponse:
    """Send `request` the guarded way and read at most MAX_RESPONSE_BYTES.

    Redirects are followed by hand (at most MAX_REDIRECTS), each hop checked
    and pinned on its own. Any HTTP status is returned, not raised: the caller
    decides what an error is. Raises HttpToolError("blocked"|"unreachable"|
    "http") for a refused address, a network failure or a redirect loop.
    `auth_headers` (lower-case) and `auth_query` are dropped on a redirect to
    another origin; `login_query` (a query-string login) is set again on a
    redirect within the origin, whose Location does not carry it.
    `same_origin_only` refuses a redirect to another origin altogether (after
    the guard has checked it, so an internal target is still `blocked`): for
    a request whose body carries credentials, which a 307/308 would resend.

    `timeout_s` bounds each network step and the whole call: a server that
    trickles one byte at a time cannot hold a call open past it.
    """
    started = time.monotonic()
    request.headers["Accept-Encoding"] = _ACCEPT_ENCODING
    token = _SENDING_TOOL_CALL.set(True)
    try:
        async with asyncio.timeout(timeout_s):
            return await _send_hops(
                request,
                started,
                timeout_s,
                auth_headers,
                auth_query,
                dict(login_query or {}),
                same_origin_only,
            )
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
    login_query: dict[str, str],
    same_origin_only: bool,
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
                            if same_origin_only:
                                await _checked_ip(str(nxt.url))
                                raise HttpToolError(
                                    "http", "The address redirected to another server."
                                )
                            nxt = _without_auth(nxt, auth_headers, auth_query)
                        elif login_query:
                            nxt = _with_query(nxt, login_query)
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


# ---- logins ------------------------------------------------------------------

#: The env name a login's one secret is resolved under (setup field
#: `auth_secret`, mapped by `secret_env_fields`; see the custom server wizard).
HTTP_AUTH_SECRET_ENV = "HTTP_AUTH_SECRET"
#: A cached token is refreshed this long before the API would reject it.
TOKEN_EXPIRY_MARGIN_S = 60.0
_DEFAULT_EXPIRES_IN_S = 3600.0

LoginKind = Literal["api_key", "basic", "oauth2_client_credentials"]


@dataclass(frozen=True)
class HttpLogin:
    """A connection's login, with its secret resolved. `repr` never shows it."""

    kind: LoginKind
    secret: str = field(repr=False)
    location: Literal["header", "query"] = "header"
    name: str = ""
    username: str = ""
    token_url: str = ""
    client_id: str = ""
    scope: str = ""


def resolve_http_login(cfg: dict[str, Any], env: dict[str, str]) -> HttpLogin | None:
    """`config.auth` plus its secret from the resolved env, or None for a
    connection without one -- including a legacy one that only declares
    `auth_header_name`, which `mcp_client.resolve_auth_header` still serves."""
    auth = cfg.get("auth")
    if not isinstance(auth, dict):
        return None
    secret = str(env.get(HTTP_AUTH_SECRET_ENV, ""))
    kind = auth.get("kind")
    if kind == "api_key":
        # A nameless key still resolves: sending it refuses the call (an
        # "auth" error) rather than going out without any login.
        name = str(auth.get("name") or "")
        location: Literal["header", "query"] = "query" if auth.get("in") == "query" else "header"
        return HttpLogin(kind="api_key", secret=secret, location=location, name=name)
    if kind == "basic":
        return HttpLogin(kind="basic", secret=secret, username=str(auth.get("username") or ""))
    if kind == "oauth2_client_credentials":
        return HttpLogin(
            kind="oauth2_client_credentials",
            secret=secret,
            token_url=str(auth.get("token_url") or ""),
            client_id=str(auth.get("client_id") or ""),
            scope=str(auth.get("scope") or ""),
        )
    return None


def _check_login(login: HttpLogin) -> None:
    """Refuse a login that cannot be sent as configured, before anything is
    sent: a call must never go out without the login it was set up with."""
    if not login.secret:
        raise HttpToolError("auth", "The login's secret is not set.")
    if login.kind == "api_key" and not login.name:
        raise HttpToolError("auth", "The login has no key name.")
    if login.kind == "basic" and ":" in login.username:
        # RFC 7617: the user-id cannot contain a colon.
        raise HttpToolError("auth", "A Basic login's username cannot contain ':'.")


def _check_header_values(headers: Mapping[str, str]) -> None:
    """Credential headers must be ASCII (httpx encodes header values as
    ASCII). Checked up front, so no codec error -- whose repr carries the
    whole value -- is ever raised or chained."""
    for value in headers.values():
        if not value.isascii():
            raise HttpToolError(
                "auth", "The login's secret contains characters a header cannot carry."
            )


def _basic_credentials(user: str, password: str) -> str:
    return base64.b64encode(f"{user}:{password}".encode()).decode()


def _client_credentials(login: HttpLogin) -> str:
    # RFC 6749 2.3.1: client id and secret are form-encoded before Basic.
    return _basic_credentials(
        urllib.parse.quote_plus(login.client_id), urllib.parse.quote_plus(login.secret)
    )


def login_secrets(login: HttpLogin | None) -> list[str]:
    """Every form in which a login's secret can travel or come back: the raw
    secret, and for Basic and the token request the base64 credentials."""
    if login is None or not login.secret:
        return []
    if login.kind == "basic":
        return [login.secret, _basic_credentials(login.username, login.secret)]
    if login.kind == "oauth2_client_credentials":
        return [login.secret, _client_credentials(login)]
    return [login.secret]


# ---- OAuth2 client credentials: one cached token per login ----------------

_TokenKey = tuple[str, str, str, str]
#: Monkeypatched by tests; monotonic so a wall-clock jump cannot keep a token.
_now: Callable[[], float] = time.monotonic
#: The longest a token is kept, whatever `expires_in` says.
_MAX_TOKEN_LIFETIME_S = 24 * 3600.0
#: The most tokens kept at once; the oldest goes first.
_MAX_TOKENS = 256
#: token per login -> (access token, monotonic time it stops being used).
_TOKENS: dict[_TokenKey, tuple[str, float]] = {}


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


#: Per event loop (weakly: a closed loop is not kept alive), one lock per
#: login, so simultaneous calls fetch one token, not one each. An entry
#: lives only while some call holds or waits for it.
_TOKEN_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[_TokenKey, _LockEntry]] = (
    weakref.WeakKeyDictionary()
)


def _token_key(login: HttpLogin) -> _TokenKey:
    # Everything that decides which token the server hands out. The secret is
    # part of the key (hashed): a rotated client secret must not keep using a
    # token minted with the old one.
    digest = hashlib.sha256(login.secret.encode()).hexdigest()
    return (login.token_url, login.client_id, login.scope, digest)


@contextlib.asynccontextmanager
async def _token_lock(key: _TokenKey) -> AsyncIterator[None]:
    loop = asyncio.get_running_loop()
    entries = _TOKEN_LOCKS.setdefault(loop, {})
    entry = entries.get(key)
    if entry is None:
        entry = entries[key] = _LockEntry(asyncio.Lock())
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0:
            entries.pop(key, None)
            if not entries:
                _TOKEN_LOCKS.pop(loop, None)


def _token_lifetime(expires_in: Any) -> float:
    """How long a token is used: `expires_in` clamped to 0..24 h (a missing,
    non-numeric or NaN value counts as an hour), less a margin of at most
    TOKEN_EXPIRY_MARGIN_S and at most half the lifetime."""
    if isinstance(expires_in, bool) or expires_in is None:
        seconds = _DEFAULT_EXPIRES_IN_S
    else:
        try:
            seconds = float(expires_in)
        except (TypeError, ValueError):
            seconds = _DEFAULT_EXPIRES_IN_S
    if math.isnan(seconds):
        seconds = _DEFAULT_EXPIRES_IN_S
    seconds = min(max(seconds, 0.0), _MAX_TOKEN_LIFETIME_S)
    return seconds - min(TOKEN_EXPIRY_MARGIN_S, seconds / 2)


def _remember_token(key: _TokenKey, token: str, lifetime_s: float) -> None:
    """Cache a token: expired entries are dropped first, and the cache never
    holds more than _MAX_TOKENS (the oldest goes)."""
    now = _now()
    for stale in [k for k, (_t, until) in _TOKENS.items() if until <= now]:
        del _TOKENS[stale]
    _TOKENS.pop(key, None)
    while len(_TOKENS) >= _MAX_TOKENS:
        del _TOKENS[next(iter(_TOKENS))]
    _TOKENS[key] = (token, now + lifetime_s)


def forget_token(login: HttpLogin) -> None:
    _TOKENS.pop(_token_key(login), None)


def _forget_rejected(login: HttpLogin, rejected: str) -> None:
    """Drop the cached token only if it is still the one the API rejected:
    a call that raced ahead may already have fetched a fresh one."""
    key = _token_key(login)
    cached = _TOKENS.get(key)
    if cached is not None and cached[0] == rejected:
        del _TOKENS[key]


async def _fetch_token(login: HttpLogin, *, timeout_s: float) -> tuple[str, float]:
    # Returns the token and how long to use it (already clamped).
    """RFC 6749 4.4: client_secret_basic first; an endpoint that refuses it
    (400/401) is asked once more with the credentials in the form
    (client_secret_post), which some providers require. The token URL goes
    through the same guard as every tool call, and a redirect to another
    origin is refused: the credentials would go along."""
    if not login.token_url:
        raise HttpToolError("token", "The login has no token URL.")
    form = {"grant_type": "client_credentials"}
    if login.scope:
        form["scope"] = login.scope
    attempts = [
        ({"Authorization": f"Basic {_client_credentials(login)}"}, form),
        ({}, {**form, "client_id": login.client_id, "client_secret": login.secret}),
    ]
    status = 0
    for extra_headers, data in attempts:
        request = httpx.Request(
            "POST",
            login.token_url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT, **extra_headers},
            data=data,
        )
        try:
            response = await send_guarded(
                request,
                timeout_s=timeout_s,
                auth_headers=frozenset({"authorization"}),
                same_origin_only=True,
            )
        except HttpToolError as exc:
            if exc.kind == "blocked":
                raise
            raise HttpToolError("token", "The token URL is not reachable.") from exc
        status = response.status
        if status in (400, 401):
            continue
        if status >= 400:
            break
        try:
            payload = json.loads(response.text)
        except ValueError:
            payload = None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise HttpToolError("token", "The token URL answered without an access token.")
        return token, _token_lifetime(payload.get("expires_in"))
    raise HttpToolError("token", f"The token request failed ({status}).", status=status or None)


async def _oauth2_token(login: HttpLogin, *, timeout_s: float, force: bool) -> tuple[str, bool]:
    """The token, and whether it was fetched by this call (not cached)."""
    _check_login(login)
    key = _token_key(login)
    async with _token_lock(key):
        cached = _TOKENS.get(key)
        if cached is not None and not force and cached[1] > _now():
            return cached[0], False
        token, lifetime_s = await _fetch_token(login, timeout_s=timeout_s)
        _remember_token(key, token, lifetime_s)
        return token, True


async def oauth2_token(login: HttpLogin, *, timeout_s: float, force: bool = False) -> str:
    """The login's access token: the cached one while it is valid, else one
    fetched now. Simultaneous callers for one login wait for one fetch."""
    token, _fresh = await _oauth2_token(login, timeout_s=timeout_s, force=force)
    return token


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
        login: HttpLogin | None = None,
    ) -> None:
        """`secrets` are raw secret values a login put into a request in a
        form no header shows verbatim (a password inside a Basic credential,
        a fetched access token); they are redacted like the header values.
        `login`'s own secrets (and every access token it fetches) are added
        to them here."""
        self._base_url = base_url.rstrip("/")
        self._login = login
        self._extra_secrets = [str(v) for v in secrets if v] + login_secrets(login)
        self._http_tools = {str(t["name"]): t for t in http_tools}
        self._headers = dict(headers or {})
        self._timeout_s = HTTP_TOOL_TIMEOUT_SECONDS if timeout_s is None else timeout_s
        self._tokens: list[str] = []
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
        return [*secret_parts(self._headers.values()), *self._extra_secrets, *self._tokens]

    def _auth_header_names(self) -> frozenset[str]:
        names = {name.lower() for name in self._headers}
        login = self._login
        if login is not None:
            if login.kind == "api_key":
                if login.location == "header":
                    names.add(login.name.lower())
            else:
                names.add("authorization")
        return frozenset(names)

    def _login_query(self) -> dict[str, str]:
        login = self._login
        if login is not None and login.kind == "api_key" and login.location == "query":
            return {login.name: login.secret}
        return {}

    async def _login_headers(self) -> tuple[dict[str, str], str]:
        """The login's headers, and the access token among them if it came
        from the cache (or ""): only a cached token is worth replacing when
        the API rejects it."""
        login = self._login
        if login is None:
            return {}, ""
        _check_login(login)
        if login.kind == "api_key":
            if login.location == "query":
                return {}, ""
            name, value = _header_pair(login.name, login.secret)
            return {name: value}, ""
        if login.kind == "basic":
            credentials = _basic_credentials(login.username, login.secret)
            return {"Authorization": f"Basic {credentials}"}, ""
        token, fresh = await _oauth2_token(login, timeout_s=self._timeout_s, force=False)
        if token not in self._tokens:
            self._tokens.append(token)
        if not token.isascii():
            raise HttpToolError("token", "The token URL answered with an unusable access token.")
        return {"Authorization": f"Bearer {token}"}, "" if fresh else token

    async def _send(
        self, tool: dict[str, Any], arguments: dict[str, Any]
    ) -> tuple[HttpResponse, str]:
        placed = place_arguments(tool, arguments, protected_headers=self._auth_header_names())
        _check_header_values(self._headers)
        login_headers, token = await self._login_headers()
        _check_header_values(login_headers)
        login_query = self._login_query()
        headers = {**placed.headers, "User-Agent": USER_AGENT, **self._headers, **login_headers}
        # The login's query key last: a tool argument of the same name cannot
        # replace it. Added to the request URL here, so it is covered by the
        # per-call log filter and by redaction like every other part.
        params = {**placed.query, **login_query}
        url = self._base_url + placed.path
        request = (
            httpx.Request(placed.method, url, headers=headers, params=params, json=placed.body)
            if placed.has_body
            else httpx.Request(placed.method, url, headers=headers, params=params)
        )
        response = await send_guarded(
            request,
            timeout_s=self._timeout_s,
            auth_headers=self._auth_header_names(),
            auth_query=frozenset(login_query),
            login_query=login_query,
        )
        return response, token

    async def request(self, name: str, arguments: dict[str, Any]) -> HttpResponse:
        """One call, any status. An OAuth2 login whose cached token the API
        rejects (401) gets one fresh token and one retry. Raises HttpToolError for a
        refused address, an unreachable API, a failed token request or a
        redirect loop. The token request, the call and its retry share one
        deadline."""
        if not self._started:
            raise RuntimeError("HTTP tool session not started")
        tool = self._http_tools.get(name)
        if tool is None:
            raise RuntimeError(f"unknown tool {name!r}")
        try:
            async with asyncio.timeout(self._timeout_s):
                response, token = await self._send(tool, arguments)
                login = self._login
                if response.status == 401 and token and login is not None:
                    # A 401 means the request was refused before it was
                    # processed, so one resend is safe for any method.
                    _forget_rejected(login, token)
                    response, _token = await self._send(tool, arguments)
                return response
        except TimeoutError as exc:
            raise HttpToolError(
                "unreachable", f"The API did not answer within {self._timeout_s:g} seconds."
            ) from exc

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

    async def try_call(self, name: str, arguments: dict[str, Any]) -> CallOutcome:
        """`request`, but every failure a person can act on comes back as an
        outcome instead of an exception: an error class, the status and a
        short excerpt with every secret removed. Raises only RuntimeError for
        an unknown tool or a missing path parameter (the caller's input)."""
        started = time.monotonic()
        try:
            response = await self.request(name, arguments)
        except HttpToolError as exc:
            # A refusal reports no time: how long the guard's lookup took (an
            # internal name answering fast, an unknown one slowly) would tell
            # internal names apart where the message deliberately does not.
            elapsed = 0 if exc.kind == "blocked" else time.monotonic() - started
            return CallOutcome(
                ok=False,
                status=exc.status,
                duration_ms=int(elapsed * 1000),
                excerpt="",
                truncated=False,
                error_class=exc.kind,
                message=redact(str(exc), self._secrets()),
            )
        # Redacted whole, then cut (see `call`).
        redacted = redact(response.text, self._secrets())
        excerpt = redacted[:TEST_EXCERPT_CHARS]
        truncated = response.truncated or len(redacted) > TEST_EXCERPT_CHARS
        message = f"The API answered {response.status}."
        if response.status >= 400:
            kind: ErrorClass = "auth" if response.status in (401, 403) else "http"
            if kind == "auth":
                message += " Check the login."
            return CallOutcome(
                False, response.status, response.duration_ms, excerpt, truncated, kind, message
            )
        return CallOutcome(
            True, response.status, response.duration_ms, excerpt, truncated, None, message
        )
