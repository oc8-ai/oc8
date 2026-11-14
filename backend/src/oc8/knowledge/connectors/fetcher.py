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


def _guard(url: str) -> str:
    """Validate the URL and return a vetted IP to connect to.

    The guard must RETURN the address the request then connects to, not merely
    assert that the hostname resolves to a safe one and let the HTTP client
    resolve it a second time. Resolving here and connecting by hostname leaves a
    TOCTOU window: an attacker-controlled name with a short TTL can answer this
    getaddrinfo with a public IP and answer the client's own lookup, milliseconds
    later, with 169.254.169.254 (DNS rebinding) -- which is exactly the bypass the
    block-list exists to stop. Returning the vetted literal IP, which the caller
    hands to the client as the connect target (Host header and TLS SNI kept as the
    original hostname), removes the second lookup and with it the window.
    """
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
    safe: str | None = None
    for ip in ips:
        if _is_blocked_ip(ip):
            raise ConnectorError(f"blocked host {host!r} -> {ip}")
        if safe is None:
            safe = ip
    if safe is None:
        raise ConnectorError(f"cannot resolve {host!r}")
    return safe


def _pinned(url: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """(connect_url, headers, extensions) that pin the request to the vetted IP.

    The connect URL carries the literal IP so the client performs no second DNS
    lookup; the original hostname rides along as the Host header and as the TLS
    SNI / certificate-verification name, so virtual hosting and HTTPS still work.
    """
    ip = _guard(url)
    parsed = urlparse(url)
    host = parsed.hostname or ""
    netloc_ip = f"[{ip}]" if ":" in ip else ip
    if parsed.port:
        netloc_ip = f"{netloc_ip}:{parsed.port}"
    connect_url = parsed._replace(netloc=netloc_ip).geturl()
    return connect_url, {"Host": host}, {"sni_hostname": host}


def _next_url(current: str, resp: httpx.Response) -> str:
    """Resolve the redirect target against the ORIGINAL hostname URL, never the
    IP-pinned connect URL -- a relative Location must keep the real host so the
    next hop is guarded and pinned the same way."""
    location = resp.headers["location"]
    return str(httpx.URL(current).join(location))


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
        connect_url, headers, extensions = _pinned(current)
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            async with client.stream(
                "GET", connect_url, headers=headers, extensions=extensions
            ) as resp:
                if resp.is_redirect and resp.has_redirect_location:
                    current = _next_url(current, resp)
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
        connect_url, headers, extensions = _pinned(current)
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            async with client.stream(
                "GET", connect_url, headers=headers, extensions=extensions
            ) as resp:
                if resp.is_redirect and resp.has_redirect_location:
                    current = _next_url(current, resp)
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
