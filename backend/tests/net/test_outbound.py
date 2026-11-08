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
