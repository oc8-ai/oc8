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
import logging
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from oc8 import __version__
from oc8.config import get_settings

_ALLOWED_SCHEMES = {"http", "https"}

#: What oc8 calls itself on outbound requests. httpx's default
#: `python-httpx/x.y` is what bot protection blocks; a stable, named agent can
#: be allowlisted by a site owner. The release version, not the plugin-compat
#: `CORE_VERSION`.
USER_AGENT = f"oc8/{__version__}"


#: Never reachable, whatever the operator allows: link-local (cloud metadata
#: lives here, as do container credential endpoints), Alibaba's metadata
#: address, and AWS's IPv6 metadata range.
_ALWAYS_BLOCKED_NETS = tuple(
    ipaddress.ip_network(n)
    for n in ("169.254.0.0/16", "fe80::/10", "100.100.100.200/32", "fd00:ec2::/32")
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")

logger = logging.getLogger(__name__)

_Addr = ipaddress.IPv4Address | ipaddress.IPv6Address


def _parse(ip: str) -> _Addr | None:
    """The address a request would really reach: an IPv4 hidden inside an
    IPv6 form (v4-mapped, NAT64, 6to4) is unwrapped so that the IPv4 rules
    apply to it."""
    try:
        addr: _Addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return addr.ipv4_mapped
        if addr in _NAT64:
            return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        if addr in _SIX_TO_FOUR:
            return ipaddress.IPv4Address((int(addr) >> 80) & 0xFFFFFFFF)
    return addr


def _always_blocked(ip: str) -> bool:
    addr = _parse(ip)
    if addr is None:
        return True
    raw = ipaddress.ip_address(ip)
    return any(
        (addr in net or raw in net) for net in _ALWAYS_BLOCKED_NETS if net.version == addr.version
    ) or any(raw in net for net in _ALWAYS_BLOCKED_NETS if net.version == raw.version)


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
    addr = _parse(ip)
    assert addr is not None
    return any(addr in net for net in nets if net.version == addr.version)


class OutboundBlocked(ValueError):
    """The URL points somewhere an outbound request must not go."""


def is_blocked_ip(ip: str) -> bool:
    addr = _parse(ip)
    if addr is None:
        return True
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


def _check_host(host: str, resolver: Callable[[str], list[str]] | None) -> str:
    resolve = resolver or resolve_ips
    try:
        ips = resolve(host)
    except OSError as exc:
        logger.info("outbound refused: cannot resolve %r", host)
        raise OutboundBlocked(f"cannot resolve {host!r}: {exc}") from exc
    if not ips:
        logger.info("outbound refused: cannot resolve %r", host)
        raise OutboundBlocked(f"cannot resolve {host!r}")
    nets, hosts = _allowed_private()
    for ip in ips:
        if not _ip_allowed(ip, host, nets, hosts):
            logger.info("outbound refused: host %r resolves to a blocked address", host)
            raise OutboundBlocked(f"blocked host {host!r} -> {ip}")
    return ips[0]


def guard_outbound_url(url: str, resolver: Callable[[str], list[str]] | None = None) -> str:
    """Return the resolved IP to connect to, or raise `OutboundBlocked`.

    Every address the host resolves to must be acceptable, not just the first.
    A value typed without a scheme ("minio:9000", "s3.example.com") is judged
    as https://<value>; the caller keeps the value as typed.
    """
    value = url.strip()
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        logger.info("outbound refused: scheme %r", parsed.scheme)
        raise OutboundBlocked(f"scheme not allowed: {parsed.scheme!r}")
    try:
        host = parsed.hostname
    except ValueError as exc:
        raise OutboundBlocked("malformed address") from exc
    if not host:
        raise OutboundBlocked("missing host")
    return _check_host(host, resolver)


def guard_outbound_host(host: str, resolver: Callable[[str], list[str]] | None = None) -> str:
    """The same rules for a bare host name (database, SMTP): scheme-agnostic."""
    value = host.strip()
    if not value:
        raise OutboundBlocked("missing host")
    return _check_host(value, resolver)


class PinnedTransport(httpx.AsyncBaseTransport):
    """Connect to a fixed, already-checked IP while keeping the original Host
    header and TLS server name, so certificate checks still use the hostname."""

    def __init__(self, ip: str, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._ip = ip
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Send a pinned copy and leave the caller's request alone: httpx keeps
        # that object as response.request and resolves a relative redirect
        # ("Location: /de") against it. Rewriting it in place made the next hop
        # https://<ip>/de, which has no SNI and fails the TLS handshake.
        host = request.url.host
        headers = httpx.Headers(request.headers)
        headers["Host"] = host if request.url.port is None else f"{host}:{request.url.port}"
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=self._ip),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return await self._inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._inner.aclose()
