from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from oc8.knowledge.connectors.base import ConnectorError

_ALLOWED_SCHEMES = {"http", "https"}


def _is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (
        addr.is_loopback or addr.is_private or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
        or not addr.is_global
    )


def _resolve_ips(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return [str(info[4][0]) for info in infos]


def _guard(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ConnectorError(f"scheme not allowed: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ConnectorError("missing host")
    try:
        ips = _resolve_ips(host)
    except OSError as exc:
        raise ConnectorError(f"cannot resolve {host!r}: {exc}") from exc
    for ip in ips:
        if _is_blocked_ip(ip):
            raise ConnectorError(f"blocked host {host!r} -> {ip}")


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
    current = url
    for _ in range(max_redirects + 1):
        _guard(current)
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
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
    current = url
    for _ in range(max_redirects + 1):
        _guard(current)
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
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
