from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from oc8.knowledge.connectors.base import ConnectorError
from oc8.knowledge.connectors.fetcher import _is_blocked_ip, safe_fetch

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "::1",
        "fe80::1",
        "fc00::1",
        "0.0.0.0",
    ],
)
def test_blocked_ips(ip: str) -> None:
    assert _is_blocked_ip(ip) is True


@pytest.mark.parametrize("ip", ["100.64.0.1", "100.127.255.255", "100.100.100.100"])
def test_cgnat_ips_blocked(ip: str) -> None:
    # RFC 6598 CGNAT range (100.64.0.0/10) is not globally routable and must be
    # blocked even though it is not covered by is_private/is_loopback/etc.
    assert _is_blocked_ip(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
def test_public_ips_allowed(ip: str) -> None:
    assert _is_blocked_ip(ip) is False


async def test_non_http_scheme_rejected() -> None:
    with pytest.raises(ConnectorError):
        await safe_fetch("file:///etc/passwd")
    with pytest.raises(ConnectorError):
        await safe_fetch("ftp://example.com/x")


async def test_blocked_host_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Resolve any host to a loopback IP -> must be blocked before any HTTP call.
    import oc8.knowledge.connectors.fetcher as f

    monkeypatch.setattr(f, "_resolve_ips", lambda host: ["127.0.0.1"])
    with pytest.raises(ConnectorError):
        await safe_fetch("http://evil.example.com/")


async def test_streaming_cap_aborts_over_max_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    # A response body larger than max_bytes must abort mid-stream (raise
    # ConnectorError) rather than being buffered fully in memory and then
    # truncated. Drive this through a mocked transport so the real
    # aiter_bytes()-based accumulation/abort logic in safe_fetch is exercised.
    import oc8.knowledge.connectors.fetcher as f

    monkeypatch.setattr(f, "_guard", lambda url: None)

    async def body_chunks() -> AsyncIterator[bytes]:
        yield b"a" * 1000
        yield b"b" * 1000
        yield b"c" * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body_chunks(), headers={"content-type": "text/plain"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    with pytest.raises(ConnectorError):
        await f.safe_fetch("http://example.com/big", max_bytes=1500)


async def test_streaming_redirect_reguards_each_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    # Redirect hops must still go through _guard() before each request, and
    # the manual redirect loop (not follow_redirects=True) must still be used
    # even though the terminal fetch is now streamed.
    import oc8.knowledge.connectors.fetcher as f

    guarded_urls: list[str] = []
    real_guard = f._guard

    def tracking_guard(url: str) -> None:
        guarded_urls.append(url)
        real_guard(url)

    monkeypatch.setattr(f, "_guard", tracking_guard)
    # Bypass DNS resolution (example.com/final.example.com aren't guaranteed
    # to resolve in the test sandbox); force a public IP for every hostname.
    monkeypatch.setattr(f, "_resolve_ips", lambda host: ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://example.com/start":
            return httpx.Response(302, headers={"location": "http://final.example.com/end"})
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    text, ctype = await f.safe_fetch("http://example.com/start")
    assert text == "ok"
    assert ctype == "text/plain"
    assert guarded_urls == ["http://example.com/start", "http://final.example.com/end"]


async def test_each_hop_connects_to_the_address_its_guard_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS answers public for the guard and private for a second lookup: the
    request must go to the public address the guard saw."""
    import oc8.knowledge.connectors.fetcher as f
    from oc8.net.outbound import PinnedTransport

    answers = iter([["93.184.216.34"], ["10.0.0.5"], ["10.0.0.5"]])
    monkeypatch.setattr("oc8.net.outbound.resolve_ips", lambda host: next(answers))
    pinned: list[object] = []
    real = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        pinned.append(kwargs["transport"])
        kwargs["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    await f.safe_fetch("http://example.test/")
    assert isinstance(pinned[0], PinnedTransport)
    assert pinned[0]._ip == "93.184.216.34"
