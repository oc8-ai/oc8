"""A small Odoo client for the knowledge connector: JSON-2 and /jsonrpc.

One interface, `OdooClient.call(model, method, **kwargs)`, two protocols.
`open_client` picks one per sync or browse:

1. `common.version` over `/jsonrpc` (no login needed, Odoo 17-19) gives the
   server version.
2. Odoo 19 or later: JSON-2, `POST {url}/json/2/{model}/{method}` with the
   secret as a bearer token, the database in `X-Odoo-Database` and the named
   arguments as the JSON body. A cheap probe confirms it works. If JSON-2
   answers 401, the secret is most likely a password rather than an API key:
   the client falls back to `/jsonrpc` and sets `hint` to
   "api_key_recommended". A 404 (JSON-2 not served) falls back silently.
3. Older than 19: `/jsonrpc` `execute_kw` after `common.authenticate`.

Every request goes through the outbound guard: the address is checked once
(`guard_outbound_url`) and the connection is pinned to it (`PinnedTransport`),
redirects are not followed, each call has one deadline, and an answer is read
in bounded steps up to MAX_RESPONSE_BYTES. No trace span is made for these
requests and httpx's request log lines are dropped while they are sent: both
would carry the Odoo address.

Errors are fixed English phrases. The url, database, login and secret never
appear in a message, a chained exception or a log line, and nothing Odoo
answers is copied into one.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import zlib
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from opentelemetry.instrumentation.utils import suppress_http_instrumentation

from oc8.knowledge.connectors.base import ConnectorError
from oc8.net.decoding import (
    ACCEPT_ENCODING,
    UnsupportedContentEncoding,
    content_decoder,
    decode_step,
)
from oc8.net.outbound import (
    USER_AGENT,
    OutboundBlocked,
    PinnedTransport,
    base_url_has_login,
    guard_outbound_url,
)

#: One deadline per request: every network step and the whole call.
TIMEOUT_SECONDS = 60.0
#: The most of one answer that is read. A 5 MiB file travels base64-encoded
#: inside JSON (about 6.7 MiB), so this leaves room for one such file.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
#: Set after a JSON-2 401: the stored secret works as a password only.
HINT_API_KEY_RECOMMENDED = "api_key_recommended"

#: The methods whose `ids`/`domain` go first among /jsonrpc's positional args.
_DOMAIN_METHODS = frozenset({"search", "search_read", "search_count"})
_MODEL_NAME = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
_METHOD_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
#: Odoo's wording when execute_kw is asked for a model it does not have; the
#: model name is captured and must be the one that was asked for.
_MISSING_MODEL = re.compile(
    r"(?:object|model) ['\"]?([a-z0-9_.]+)['\"]? (?:doesn't|does not) exist", re.IGNORECASE
)
#: The JSON-2 404 that means "no such model or method", as opposed to a 404
#: for a record that is gone (MissingError).
_NOT_FOUND_NAMES = frozenset({"", "werkzeug.exceptions.NotFound"})

Protocol = Literal["json2", "jsonrpc"]
App = Literal["documents", "knowledge"]

logger = logging.getLogger(__name__)

# httpx logs every request URL at INFO ("HTTP Request: POST https://<odoo>/..."),
# and httpcore logs connection targets at DEBUG. The Odoo address is
# configuration that must not reach a log, so those lines are dropped while
# this client sends. A logger filter does not apply to child loggers, so each
# httpcore logger is named.
_SENDING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "oc8_odoo_client_sending", default=False
)


class _DropOdooRequestLines(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _SENDING.get()


_QUIET_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
for _name in _QUIET_LOGGERS:
    logging.getLogger(_name).addFilter(_DropOdooRequestLines())


class OdooError(ConnectorError):
    """A failure talking to Odoo. The message is a fixed phrase."""

    default_message = "Odoo refused the request."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class OdooLoginRefused(OdooError):
    default_message = "Odoo refused the login."


class OdooAccessDenied(OdooError):
    default_message = "The Odoo login has no access to this."


class OdooAppMissing(OdooError):
    default_message = "The Odoo app is not installed."


class OdooRecordMissing(OdooError):
    default_message = "The Odoo record does not exist anymore."


class OdooUnreachable(OdooError):
    default_message = "Odoo is not reachable."


class _JsonRpcGone(OdooUnreachable):
    """/jsonrpc answered 404: an Odoo that no longer serves it."""


class _Answer:
    """One HTTP answer: status and decoded JSON (None when it is not JSON)."""

    __slots__ = ("body", "status")

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body


def _error_name(body: Any) -> str:
    """The Odoo exception name in an error body (JSON-2: top level; JSON-RPC:
    `error.data.name`), or ""."""
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        data = error.get("data")
        name = data.get("name") if isinstance(data, dict) else None
    else:
        name = body.get("name")
    return name if isinstance(name, str) else ""


def _error_message(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        data = error.get("data")
        message = data.get("message") if isinstance(data, dict) else None
    else:
        message = body.get("message")
    return message if isinstance(message, str) else ""


def _classify(name: str, message: str, model: str) -> OdooError | None:
    """Map an Odoo exception to a connector error. Only the name and a fixed
    pattern are looked at; nothing of Odoo's text is carried over.

    `OdooAppMissing` here is a best effort: it is raised only when Odoo names
    the very model that was asked for. Callers that need to know whether an
    app is installed ask `OdooClient.model_exists` instead.
    """
    short = name.rsplit(".", 1)[-1]
    if short == "AccessError":
        return OdooAccessDenied()
    if short == "AccessDenied":
        return OdooLoginRefused()
    if short == "MissingError":
        return OdooRecordMissing()
    if model:
        named = _MISSING_MODEL.search(message)
        if named is not None and named.group(1) == model:
            return OdooAppMissing()
        if short == "KeyError" and message.strip().strip("'\"") == model:
            return OdooAppMissing()
    return None


def _parse_version(info: Any) -> tuple[int, int]:
    """`server_version_info`, e.g. [19, 0, 0, "final", 0, ""], or for an
    Odoo Online release, whose major carries a tilde-prefixed tag."""
    try:
        major = int(str(info[0]).rsplit("~", 1)[-1])
        minor = int(info[1])
    except (TypeError, ValueError, IndexError, KeyError):
        raise OdooError("Odoo did not report its version.") from None
    return major, minor


def _jsonrpc_args(method: str, kwargs: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    """/jsonrpc takes a record's ids, and a search's domain, positionally."""
    rest = dict(kwargs)
    if "ids" in rest:
        return [rest.pop("ids")], rest
    if method in _DOMAIN_METHODS and "domain" in rest:
        return [rest.pop("domain")], rest
    return [], rest


class OdooClient:
    """An authenticated connection to one Odoo database. Build it with
    `open_client`; close it with `aclose`."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        base_url: str,
        database: str,
        login: str,
        secret: str,
    ) -> None:
        self._http = http
        self._base_url = base_url
        self._database = database
        self._login = login
        self._secret = secret
        self._uid: int | None = None
        self._jsonrpc_served = True
        self.version: tuple[int, int] = (0, 0)
        self.protocol: Protocol = "jsonrpc"
        self.hint: str | None = None

    def __repr__(self) -> str:
        return f"OdooClient(version={self.version!r}, protocol={self.protocol!r})"

    # ---- transport -----------------------------------------------------------

    async def _post(self, path: str, body: Any, headers: dict[str, str] | None = None) -> _Answer:
        """POST one JSON body and read the answer, guarded and bounded.
        Raises OdooUnreachable for a network failure, a timeout or a redirect."""
        token = _SENDING.set(True)
        failure: OdooError | None = None
        answer: _Answer | None = None
        try:
            with suppress_http_instrumentation():
                async with asyncio.timeout(TIMEOUT_SECONDS):
                    request = self._http.build_request(
                        "POST",
                        f"{self._base_url}{path}",
                        content=json.dumps(body).encode(),
                        headers={
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                            "Accept-Encoding": ACCEPT_ENCODING,
                            **(headers or {}),
                        },
                    )
                    answer = await self._send(request)
        except OdooError as exc:
            failure = exc
        except (TimeoutError, httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.info("odoo request failed: %s", type(exc).__name__)
            failure = OdooUnreachable()
        finally:
            _SENDING.reset(token)
        # Raised outside the except block, so no httpx exception (whose text
        # names the address) is chained to it.
        if failure is not None:
            failure.__context__ = None
            raise failure
        assert answer is not None
        return answer

    async def _send(self, request: httpx.Request) -> _Answer:
        response = await self._http.send(request, stream=True)
        try:
            if response.is_redirect:
                logger.info("odoo answered with a redirect; not followed")
                raise OdooUnreachable()
            raw = await self._read_capped(response)
        finally:
            await response.aclose()
        try:
            body = json.loads(raw.decode(response.encoding or "utf-8", errors="replace"))
        except ValueError:
            body = None
        return _Answer(response.status_code, body)

    @staticmethod
    async def _read_capped(response: httpx.Response) -> bytes:
        try:
            decoder = content_decoder(response.headers.get("content-encoding", ""))
        except UnsupportedContentEncoding:
            raise OdooError("Odoo answered in an unsupported encoding.") from None
        buffer = bytearray()
        received = False
        try:
            async for chunk in response.aiter_raw():
                received = received or bool(chunk)
                out, overflow = decode_step(decoder, chunk, MAX_RESPONSE_BYTES - len(buffer))
                buffer.extend(out)
                if overflow:
                    raise OdooError("The Odoo answer is too large.")
        except zlib.error:
            raise OdooError("The Odoo answer cannot be decoded.") from None
        if received and not decoder.eof:
            raise OdooError("The Odoo answer ended early.")
        return bytes(buffer)

    # ---- /jsonrpc ------------------------------------------------------------

    async def _jsonrpc(self, service: str, method: str, args: list[Any], model: str = "") -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
        }
        answer = await self._post("/jsonrpc", payload)
        if answer.status == 404:
            logger.info("odoo does not serve /jsonrpc")
            raise _JsonRpcGone()
        if answer.status != 200:
            logger.info("odoo /jsonrpc answered with HTTP %s", answer.status)
            raise OdooUnreachable()
        body = answer.body
        if not isinstance(body, dict):
            raise OdooError("Odoo did not answer with JSON-RPC.")
        if body.get("error") is not None:
            name = _error_name(body)
            logger.info("odoo /jsonrpc call failed: %s", name or "unknown error")
            raise _classify(name, _error_message(body), model) or OdooError()
        return body.get("result")

    async def _read_version(self) -> tuple[int, int]:
        """`common.version` over /jsonrpc; where /jsonrpc is gone (a later
        Odoo), the web client's JSON-RPC route `/web/webclient/version_info`,
        which answers the same. (`/web/version` is a GET-only http route and
        answers a JSON-RPC POST with a 400 page.)"""
        try:
            result = await self._jsonrpc("common", "version", [])
        except _JsonRpcGone:
            self._jsonrpc_served = False
            answer = await self._post(
                "/web/webclient/version_info",
                {"jsonrpc": "2.0", "method": "call", "params": {}},
            )
            body = answer.body if answer.status == 200 else None
            result = body.get("result") if isinstance(body, dict) else None
        info = None
        if isinstance(result, dict):
            # Both routes name it `server_version_info`; accept `version_info`
            # too, the name `/web/version` uses.
            info = result.get("server_version_info") or result.get("version_info")
        return _parse_version(info)

    async def _jsonrpc_login(self) -> None:
        uid = await self._jsonrpc(
            "common", "authenticate", [self._database, self._login, self._secret, {}]
        )
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            raise OdooLoginRefused()
        self._uid = uid

    # ---- JSON-2 --------------------------------------------------------------

    async def _json2(self, model: str, method: str, kwargs: dict[str, Any]) -> _Answer:
        return await self._post(
            f"/json/2/{model}/{method}",
            kwargs,
            headers={
                "Authorization": f"bearer {self._secret}",
                "X-Odoo-Database": self._database,
            },
        )

    @staticmethod
    def _json2_result(answer: _Answer, model: str = "") -> Any:
        if answer.status == 200:
            return answer.body
        name = _error_name(answer.body)
        logger.info("odoo JSON-2 call failed: HTTP %s %s", answer.status, name or "")
        mapped = _classify(name, _error_message(answer.body), model) if name else None
        if mapped is not None:
            raise mapped
        if answer.status == 401:
            raise OdooLoginRefused()
        if answer.status == 403:
            raise OdooAccessDenied()
        if answer.status == 404 and name in _NOT_FOUND_NAMES:
            raise OdooAppMissing()
        if answer.status >= 500:
            raise OdooUnreachable()
        raise OdooError()

    async def _select_protocol(self) -> None:
        self.version = await self._read_version()
        if self.version[0] >= 19:
            # A cheap call any login may make, to see whether JSON-2 takes the secret.
            probe = await self._json2("res.users", "context_get", {})
            if probe.status == 200:
                self.protocol = "json2"
                await self._bind_key_to_login(probe.body)
                return
            if probe.status == 401:
                self.hint = HINT_API_KEY_RECOMMENDED
            elif probe.status != 404:
                self._json2_result(probe)
        self.protocol = "jsonrpc"
        await self._jsonrpc_login()

    async def _bind_key_to_login(self, context: Any) -> None:
        """JSON-2 acts as whoever owns the key, whatever login was stored.
        Check once over /jsonrpc that the login and the secret belong
        together, and, when the probe told us its uid, that it is the same
        user. A later Odoo without /jsonrpc skips the check."""
        if not self._jsonrpc_served:
            return
        try:
            await self._jsonrpc_login()
        except _JsonRpcGone:
            return
        uid = context.get("uid") if isinstance(context, dict) else None
        if isinstance(uid, int) and not isinstance(uid, bool) and uid != self._uid:
            logger.info("odoo API key belongs to another user than the stored login")
            raise OdooLoginRefused()

    # ---- public --------------------------------------------------------------

    async def call(self, model: str, method: str, **kwargs: Any) -> Any:
        """Call `model.method` with named arguments, over whichever protocol
        was chosen. `ids` (and a search's `domain`) are placed as /jsonrpc
        expects them."""
        if not _MODEL_NAME.match(model) or not _METHOD_NAME.match(method):
            raise OdooError("Not a valid Odoo model or method name.")
        if method in _DOMAIN_METHODS and "ids" in kwargs and "domain" in kwargs:
            raise ValueError("pass either ids or domain to a search, not both")
        if self.protocol == "json2":
            return self._json2_result(await self._json2(model, method, kwargs), model)
        args, rest = _jsonrpc_args(method, kwargs)
        return await self._jsonrpc(
            "object",
            "execute_kw",
            [self._database, self._uid, self._secret, model, method, args, rest],
            model,
        )

    async def model_exists(self, model: str) -> bool:
        """Whether the model is installed (an `ir.model` record exists)."""
        count = await self.call("ir.model", "search_count", domain=[["model", "=", model]])
        return isinstance(count, int) and count > 0

    async def can_read(self, model: str) -> bool:
        """Whether the login may read `model` at all. Record rules still
        decide which records it sees."""
        try:
            await self.call(model, "search", domain=[], limit=1)
        except OdooAccessDenied:
            return False
        return True

    def record_url(self, app: App, record_id: int) -> str:
        """A link to the record in Odoo's web client, by version."""
        model = "documents.document" if app == "documents" else "knowledge.article"
        if self.version[0] >= 18:
            return f"{self._base_url}/odoo/{app}/{int(record_id)}"
        return f"{self._base_url}/web#model={model}&id={int(record_id)}&view_type=form"

    async def aclose(self) -> None:
        await self._http.aclose()


async def open_client(
    *,
    url: str,
    database: str,
    login: str,
    secret: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OdooClient:
    """Check the address, pick the protocol and log in once.

    `transport` is for tests: it replaces the network beneath the pinned
    transport. The outbound guard runs either way.
    """
    base_url = url.strip().rstrip("/")
    if not base_url or base_url_has_login(base_url):
        raise OdooError("The Odoo address is not valid.")
    malformed = False
    try:
        scheme = urlsplit(base_url).scheme.lower()
    except ValueError:
        malformed = True
    if malformed:
        # Raised outside the except block, so nothing naming the address is
        # chained to it.
        logger.info("odoo address refused: not a valid address")
        raise OdooUnreachable("The Odoo address is not valid.")
    if scheme not in ("http", "https"):
        logger.info("odoo address refused: not an http(s) address")
        raise OdooUnreachable("The Odoo address is not allowed.")
    if not database or not login or not secret:
        raise OdooError("The Odoo login is incomplete.")
    blocked = False
    try:
        # getaddrinfo blocks; keep it off the event loop.
        ip = await asyncio.to_thread(guard_outbound_url, base_url)
    except OutboundBlocked:
        blocked = True
    if blocked:
        logger.info("odoo address refused by the outbound guard")
        raise OdooUnreachable("The Odoo address is not allowed.")
    http = httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
        transport=PinnedTransport(ip, inner=transport),
        headers={"User-Agent": USER_AGENT},
    )
    client = OdooClient(http=http, base_url=base_url, database=database, login=login, secret=secret)
    try:
        await client._select_protocol()
    except BaseException:
        await client.aclose()
        raise
    return client
