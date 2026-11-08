from __future__ import annotations

import httpx
import pytest

from oc8.net.outbound import OutboundBlocked, PinnedTransport, guard_outbound_url

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1", "http://[::ffff:127.0.0.1]", "http://169.254.169.254", "gopher://x"]
)
def test_blocked(url: str) -> None:
    with pytest.raises(OutboundBlocked):
        guard_outbound_url(url, resolver=lambda h: [h])


def test_unresolvable_is_blocked() -> None:
    def fail(host: str) -> list[str]:
        raise OSError("no such host")

    with pytest.raises(OutboundBlocked):
        guard_outbound_url("https://nope.test", resolver=fail)


async def test_pinned_transport_connects_to_the_ip_but_keeps_host_and_sni() -> None:
    seen: list[httpx.Request] = []

    class Stub(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, request=request)

    transport = PinnedTransport("93.184.216.34")
    transport._inner = Stub()  # type: ignore[assignment]
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post("https://odoo.acme.test:8443/jsonrpc", json={})
    req = seen[0]
    assert req.url.host == "93.184.216.34"
    assert req.headers["Host"] == "odoo.acme.test:8443"
    assert req.extensions["sni_hostname"] == "odoo.acme.test"


def _allow(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    from oc8.config import get_settings

    monkeypatch.setattr(get_settings(), "outbound_allowed_private", value)


def test_private_targets_are_refused_by_default() -> None:
    with pytest.raises(OutboundBlocked):
        guard_outbound_url("http://odoo.lan:8069", resolver=lambda h: ["10.1.2.3"])


def test_an_allowed_cidr_lets_a_private_address_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow(monkeypatch, "10.1.0.0/16, 192.168.0.0/24")
    assert guard_outbound_url("http://odoo.lan", resolver=lambda h: ["10.1.2.3"]) == "10.1.2.3"
    with pytest.raises(OutboundBlocked):
        guard_outbound_url("http://odoo.lan", resolver=lambda h: ["10.2.0.1"])


def test_an_allowed_hostname_lets_it_through_but_only_that_exact_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow(monkeypatch, "host.docker.internal,minio")
    assert guard_outbound_url("http://host.docker.internal:1000", resolver=lambda h: ["172.17.0.1"])
    assert guard_outbound_url("http://minio:9000", resolver=lambda h: ["172.18.0.5"])
    with pytest.raises(OutboundBlocked):
        guard_outbound_url("http://evil.minio", resolver=lambda h: ["172.18.0.5"])
    with pytest.raises(OutboundBlocked):
        guard_outbound_url("http://other.lan", resolver=lambda h: ["172.18.0.5"])


def test_the_metadata_address_is_refused_even_when_its_range_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow(monkeypatch, "169.254.0.0/16,fd00:ec2::/32,metadata.lan")
    for ip in ("169.254.169.254", "fd00:ec2::254"):
        with pytest.raises(OutboundBlocked):
            guard_outbound_url("http://metadata.lan", resolver=lambda h, ip=ip: [ip])
    with pytest.raises(OutboundBlocked):  # the whole link-local range, not just the one address
        guard_outbound_url("http://x.lan", resolver=lambda h: ["169.254.1.1"])


def test_a_url_without_a_scheme_is_checked_as_https() -> None:
    assert (
        guard_outbound_url("s3.example.com", resolver=lambda h: ["93.184.216.34"])
        == "93.184.216.34"
    )
    assert (
        guard_outbound_url("minio:9000/x", resolver=lambda h: ["93.184.216.34"]) == "93.184.216.34"
    )
    with pytest.raises(OutboundBlocked):
        guard_outbound_url("localhost:9000", resolver=lambda h: ["127.0.0.1"])


@pytest.mark.parametrize(
    "ip",
    [
        "169.254.170.2",
        "169.254.169.254",
        "fe80::1",
        "100.100.100.200",
        "fd00:ec2::254",
        "fd00:ec2::1",
        "64:ff9b::a00:1",  # NAT64 around 10.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 around 169.254.169.254
        "2002:0a00:0001::1",  # 6to4 around 10.0.0.1
        "2002:a9fe:a9fe::1",  # 6to4 around 169.254.169.254
    ],
)
def test_link_local_and_metadata_ranges_are_refused_even_when_allowed(
    monkeypatch: pytest.MonkeyPatch, ip: str
) -> None:
    _allow(monkeypatch, "169.254.0.0/16,fe80::/10,100.64.0.0/10,fd00:ec2::/32,meta.lan")
    with pytest.raises(OutboundBlocked):
        guard_outbound_url("http://x.lan", resolver=lambda h: [ip])
    if ip.startswith(("169.254", "fe80", "100.100", "fd00:ec2")):  # even an allowed hostname
        with pytest.raises(OutboundBlocked):
            guard_outbound_url("http://meta.lan", resolver=lambda h: [ip])


def test_an_embedded_public_address_is_judged_as_that_address() -> None:
    assert guard_outbound_url("http://x.test", resolver=lambda h: ["64:ff9b::808:808"])
    with pytest.raises(OutboundBlocked):
        guard_outbound_url("http://x.test", resolver=lambda h: ["64:ff9b::7f00:1"])


def test_a_bare_host_is_resolved_and_judged_like_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8.net.outbound import guard_outbound_host

    assert guard_outbound_host("db.example.com", resolver=lambda h: ["93.184.216.34"])
    with pytest.raises(OutboundBlocked):
        guard_outbound_host("db.internal", resolver=lambda h: ["10.0.0.9"])
    _allow(monkeypatch, "db.internal")
    assert guard_outbound_host("db.internal", resolver=lambda h: ["10.0.0.9"]) == "10.0.0.9"
