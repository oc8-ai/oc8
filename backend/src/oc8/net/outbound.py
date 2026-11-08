"""Guard for outbound requests to a URL a person typed in.

Refuses anything that is not a plain http(s) URL to a globally routable
address (loopback, private, link-local incl. the cloud metadata address,
CGNAT, reserved, multicast; IPv4 and IPv6), and hands back the address that
was checked so the caller can connect to exactly that one (`PinnedTransport`):
a second DNS lookup at connect time could otherwise answer differently
(DNS rebinding).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse

import httpx

_ALLOWED_SCHEMES = {"http", "https"}


class OutboundBlocked(ValueError):
    """The URL points somewhere an outbound request must not go."""


def is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or not addr.is_global
    )


def resolve_ips(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return [str(info[4][0]) for info in infos]


def guard_outbound_url(url: str, resolver: Callable[[str], list[str]] | None = None) -> str:
    """Return the resolved IP to connect to, or raise `OutboundBlocked`.

    Every address the host resolves to must be global, not just the first.
    """
    resolve = resolver or resolve_ips
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise OutboundBlocked(f"scheme not allowed: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise OutboundBlocked("missing host")
    try:
        ips = resolve(host)
    except OSError as exc:
        raise OutboundBlocked(f"cannot resolve {host!r}: {exc}") from exc
    if not ips:
        raise OutboundBlocked(f"cannot resolve {host!r}")
    for ip in ips:
        if is_blocked_ip(ip):
            raise OutboundBlocked(f"blocked host {host!r} -> {ip}")
    return ips[0]


class PinnedTransport(httpx.AsyncBaseTransport):
    """Connect to a fixed, already-checked IP while keeping the original Host
    header and TLS server name, so certificate checks still use the hostname."""

    def __init__(self, ip: str) -> None:
        self._ip = ip
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        request.headers["Host"] = host if request.url.port is None else f"{host}:{request.url.port}"
        request.extensions = {**request.extensions, "sni_hostname": host}
        request.url = request.url.copy_with(host=self._ip)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
