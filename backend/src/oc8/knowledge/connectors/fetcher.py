"""The guarded fetch for URLs a person typed in: pages and archives.

A refusal is a `FetchError` (a `ConnectorError`, so existing callers keep
catching it) whose subclass and `code` say why, so a caller never has to
read the message: `blocked`, `unreachable`, `too_large` or `timeout`.
"""

from __future__ import annotations

import zlib
from collections.abc import Mapping
from typing import ClassVar, Literal

import httpx

from oc8.knowledge.connectors.base import ConnectorError
from oc8.net import outbound
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
    guard_outbound_url,
    is_blocked_ip,
)

_is_blocked_ip = is_blocked_ip

FetchErrorCode = Literal["blocked", "unreachable", "too_large", "timeout"]


class FetchError(ConnectorError):
    """A guarded fetch was refused or failed. `code` names why."""

    code: ClassVar[FetchErrorCode]


class FetchBlocked(FetchError):
    """The URL (or a redirect hop) is not a public http(s) address."""

    code = "blocked"


class FetchUnreachable(FetchError):
    """Network or TLS failure, too many redirects, an undecodable body, or (for
    `safe_fetch_bytes`) an HTTP error status, which is then `status_code`."""

    code = "unreachable"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FetchTooLarge(FetchError):
    """The body grew past `max_bytes` (aborted mid-stream)."""

    code = "too_large"


class FetchTimeout(FetchError):
    """A network step took longer than `timeout`."""

    code = "timeout"


def _as_fetch_error(url: str, exc: httpx.HTTPError) -> FetchError:
    if isinstance(exc, httpx.TimeoutException):
        return FetchTimeout(f"timed out fetching {url!r}: {exc}")
    if isinstance(exc, httpx.HTTPStatusError):
        return FetchUnreachable(
            f"could not fetch {url!r}: {exc}", status_code=exc.response.status_code
        )
    return FetchUnreachable(f"could not fetch {url!r}: {exc}")


def _resolve_ips(host: str) -> list[str]:
    return outbound.resolve_ips(host)


def _guard(url: str) -> str | None:
    """Refuse a blocked target; return the address that was checked."""
    try:
        return guard_outbound_url(url, resolver=_resolve_ips)
    except OutboundBlocked as exc:
        raise FetchBlocked(str(exc)) from exc


def _request_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """oc8's User-Agent and the encodings decoded here, then the caller's
    headers on top. The caller may not set Host: the guard checked the URL's
    host, and the connection is pinned to its address."""
    merged = {"User-Agent": USER_AGENT, "Accept-Encoding": ACCEPT_ENCODING}
    for name, value in (headers or {}).items():
        if name.strip().lower() == "host":
            raise ValueError("a fetch may not set the Host header")
        for key in [k for k in merged if k.lower() == name.strip().lower()]:
            del merged[key]
        merged[name] = value
    return merged


async def safe_fetch_bytes(
    url: str,
    *,
    max_bytes: int = 20_000_000,
    max_redirects: int = 5,
    timeout: float = 30.0,  # noqa: ASYNC109 -- mirrors safe_fetch's public kwarg
    headers: Mapping[str, str] | None = None,
) -> bytes:
    """The same guard, for content that is not text.

    A repository archive is bytes and is bigger than a page, so it needs its own
    ceilings -- but not its own guard: every redirect hop is re-checked against
    the same block-list, because a public host that answers with a redirect to
    169.254.169.254 is the whole reason the guard exists.

    `headers` are sent on every hop (an `Accept`, an `Accept-Encoding`); an
    encoding other than gzip or deflate in the answer is `FetchUnreachable`.
    An HTTP error status is `FetchUnreachable` with its `status_code`.
    """
    request_headers = _request_headers(headers)
    try:
        body, _ = await _fetch(
            url,
            max_bytes=max_bytes,
            max_redirects=max_redirects,
            timeout=timeout,
            headers=request_headers,
            raise_for_status=True,
            what="archive at",
        )
    except httpx.HTTPError as exc:
        raise _as_fetch_error(url, exc) from exc
    return body


async def _fetch(
    url: str,
    *,
    max_bytes: int,
    max_redirects: int,
    timeout: float,  # noqa: ASYNC109
    headers: dict[str, str],
    raise_for_status: bool,
    what: str,
) -> tuple[bytes, httpx.Response]:
    """GET `url`, guarding and pinning each redirect hop, and read at most
    `max_bytes` of the decoded body. The raw bytes are decoded here in steps
    that never yield more than what still fits, so a small compressed body
    cannot inflate past the budget in memory."""
    current = url
    for _ in range(max_redirects + 1):
        ip = _guard(current)
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            headers=headers,
            transport=PinnedTransport(ip) if ip else None,
        ) as client:
            async with client.stream("GET", current) as resp:
                if resp.is_redirect and resp.has_redirect_location:
                    current = (
                        str(resp.next_request.url)
                        if resp.next_request
                        else resp.headers["location"]
                    )
                    continue
                if raise_for_status:
                    resp.raise_for_status()
                encoding = resp.headers.get("content-encoding", "")
                try:
                    decoder = content_decoder(encoding)
                except UnsupportedContentEncoding:
                    raise FetchUnreachable(
                        f"{current!r} answered with an unsupported content encoding ({encoding})"
                    ) from None
                buffer = bytearray()
                try:
                    async for chunk in resp.aiter_raw():
                        out, overflow = decode_step(decoder, chunk, max_bytes - len(buffer))
                        buffer.extend(out)
                        if overflow:
                            raise FetchTooLarge(
                                f"{what} {current!r} exceeded max_bytes={max_bytes} "
                                "(aborted mid-stream)"
                            )
                except zlib.error as exc:
                    raise FetchUnreachable(
                        f"{current!r} answered with a body that cannot be decoded"
                    ) from exc
                return bytes(buffer), resp
    raise FetchUnreachable(f"too many redirects for {url!r}")


async def safe_fetch(
    url: str,
    *,
    max_bytes: int = 5_000_000,
    max_redirects: int = 3,
    timeout: float = 10.0,  # noqa: ASYNC109 -- public kwarg per the connector interface, not a cancel scope
    headers: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Fetch a page; a network or TLS failure is a ConnectorError, so a caller
    that skips unreachable pages (a website sync) does not abort instead.
    `headers` as for `safe_fetch_bytes`; an HTTP error status is not raised."""
    request_headers = _request_headers(headers)
    try:
        body, resp = await _fetch(
            url,
            max_bytes=max_bytes,
            max_redirects=max_redirects,
            timeout=timeout,
            headers=request_headers,
            raise_for_status=False,
            what="response for",
        )
    except httpx.HTTPError as exc:
        raise _as_fetch_error(url, exc) from exc
    ctype = resp.headers.get("content-type", "text/html").split(";")[0].strip()
    return body.decode(resp.encoding or "utf-8", errors="replace"), ctype
