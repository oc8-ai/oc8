from __future__ import annotations

import httpx

from oc8.knowledge.connectors.base import ConnectorError
from oc8.net import outbound
from oc8.net.outbound import OutboundBlocked, PinnedTransport, guard_outbound_url, is_blocked_ip

_is_blocked_ip = is_blocked_ip


def _resolve_ips(host: str) -> list[str]:
    return outbound.resolve_ips(host)


def _guard(url: str) -> str | None:
    """Refuse a blocked target; return the address that was checked."""
    try:
        return guard_outbound_url(url, resolver=_resolve_ips)
    except OutboundBlocked as exc:
        raise ConnectorError(str(exc)) from exc


async def safe_fetch_bytes(
    url: str,
    *,
    max_bytes: int = 20_000_000,
    max_redirects: int = 5,
    timeout: float = 30.0,  # noqa: ASYNC109 -- mirrors safe_fetch's public kwarg
) -> bytes:
    """The same guard, for content that is not text.

    A repository archive is bytes and is bigger than a page, so it needs its own
    ceilings -- but not its own guard: every redirect hop is re-checked against
    the same block-list, because a public host that answers with a redirect to
    169.254.169.254 is the whole reason the guard exists.
    """
    try:
        return await _fetch_bytes(
            url, max_bytes=max_bytes, max_redirects=max_redirects, timeout=timeout
        )
    except httpx.HTTPError as exc:
        raise ConnectorError(f"could not fetch {url!r}: {exc}") from exc


async def _fetch_bytes(
    url: str,
    *,
    max_bytes: int,
    max_redirects: int,
    timeout: float,  # noqa: ASYNC109
) -> bytes:
    current = url
    for _ in range(max_redirects + 1):
        ip = _guard(current)
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
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
                resp.raise_for_status()
                buffer = bytearray()
                async for chunk in resp.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        raise ConnectorError(
                            f"archive at {current!r} exceeded max_bytes={max_bytes} "
                            "(aborted mid-stream)"
                        )
                return bytes(buffer)
    raise ConnectorError(f"too many redirects for {url!r}")


async def safe_fetch(
    url: str,
    *,
    max_bytes: int = 5_000_000,
    max_redirects: int = 3,
    timeout: float = 10.0,  # noqa: ASYNC109 -- public kwarg per the connector interface, not a cancel scope
) -> tuple[str, str]:
    """Fetch a page; a network or TLS failure is a ConnectorError, so a caller
    that skips unreachable pages (a website sync) does not abort instead."""
    try:
        return await _fetch_text(
            url, max_bytes=max_bytes, max_redirects=max_redirects, timeout=timeout
        )
    except httpx.HTTPError as exc:
        raise ConnectorError(f"could not fetch {url!r}: {exc}") from exc


async def _fetch_text(
    url: str,
    *,
    max_bytes: int,
    max_redirects: int,
    timeout: float,  # noqa: ASYNC109
) -> tuple[str, str]:
    current = url
    for _ in range(max_redirects + 1):
        ip = _guard(current)
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
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
                buffer = bytearray()
                async for chunk in resp.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        raise ConnectorError(
                            f"response for {current!r} exceeded max_bytes={max_bytes} "
                            "(aborted mid-stream)"
                        )
                ctype = resp.headers.get("content-type", "text/html").split(";")[0].strip()
                return bytes(buffer).decode(resp.encoding or "utf-8", errors="replace"), ctype
    raise ConnectorError(f"too many redirects for {url!r}")
