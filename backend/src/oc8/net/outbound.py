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
from typing import Any
from urllib.parse import urlparse

import httpx

from oc8.config import get_settings

_ALLOWED_SCHEMES = {"http", "https"}


#: Never reachable, whatever the operator allows: cloud metadata services.
_ALWAYS_BLOCKED = (
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
)


def _always_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr in _ALWAYS_BLOCKED


def _allowed_private() -> tuple[list[ipaddress.IPv4Network | ipaddress.IPv6Network], set[str]]:
    """The operator's allowlist (OC8_OUTBOUND_ALLOWED_PRIVATE): networks and
    exact hostnames. An unparsable entry is treated as a hostname."""
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    hosts: set[str] = set()
    for raw in get_settings().outbound_allowed_private.split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            hosts.add(entry)
    return nets, hosts


def _ip_allowed(ip: str, host: str, nets: list[Any], hosts: set[str]) -> bool:
    if _always_blocked(ip):
        return False
    if not is_blocked_ip(ip):
        return True
    if host.lower() in hosts:
        return True
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in nets if net.version == addr.version)


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
    nets, hosts = _allowed_private()
    for ip in ips:
        if not _ip_allowed(ip, host, nets, hosts):
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
