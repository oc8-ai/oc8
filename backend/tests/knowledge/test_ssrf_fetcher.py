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


def _pin_through(
    monkeypatch: pytest.MonkeyPatch, handler: object, dns: dict[str, str]
) -> list[str]:
    """Route every pinned connection to `handler` and resolve names from `dns`;
    return the URLs the guard was asked about, in order."""
    import oc8.knowledge.connectors.fetcher as f
    from oc8.net.outbound import PinnedTransport

    def resolve(host: str) -> list[str]:
        return [dns.get(host, host)]

    monkeypatch.setattr(f, "_resolve_ips", resolve)
    guarded: list[str] = []
    real_guard = f._guard

    def tracking_guard(url: str) -> str | None:
        guarded.append(url)
        return real_guard(url)

    monkeypatch.setattr(f, "_guard", tracking_guard)
    monkeypatch.setattr(
        f,
        "PinnedTransport",
        lambda ip: PinnedTransport(ip, inner=httpx.MockTransport(handler)),  # type: ignore[arg-type]
    )
    return guarded


async def test_a_relative_redirect_stays_on_the_hostname_not_the_pinned_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """https://www.example.org/ answers 301 "/de": the next hop must be
    https://www.example.org/de, not https://<pinned ip>/de (no SNI on an IP,
    so the TLS handshake fails)."""
    import oc8.knowledge.connectors.fetcher as f

    hops: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append((request.url.host, request.headers["Host"], request.extensions["sni_hostname"]))
        if request.url.path == "/":
            return httpx.Response(301, headers={"location": "/de"})
        return httpx.Response(200, text="hallo", headers={"content-type": "text/html"})

    guarded = _pin_through(monkeypatch, handler, {"www.example.test": "93.184.216.34"})
    text, _ = await f.safe_fetch("https://www.example.test/")
    assert text == "hallo"
    assert guarded == ["https://www.example.test/", "https://www.example.test/de"]
    assert hops == [
        ("93.184.216.34", "www.example.test", "www.example.test"),
        ("93.184.216.34", "www.example.test", "www.example.test"),
    ]


async def test_bytes_relative_redirect_stays_on_the_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import oc8.knowledge.connectors.fetcher as f

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/archive":
            return httpx.Response(302, headers={"location": "/archive.tar.gz"})
        return httpx.Response(200, content=b"\x1f\x8b")

    guarded = _pin_through(monkeypatch, handler, {"code.example.test": "93.184.216.34"})
    assert await f.safe_fetch_bytes("https://code.example.test/archive") == b"\x1f\x8b"
    assert guarded[-1] == "https://code.example.test/archive.tar.gz"


async def test_an_absolute_redirect_to_a_private_host_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import oc8.knowledge.connectors.fetcher as f

    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request.headers["Host"])
        return httpx.Response(301, headers={"location": "http://internal.example.test/admin"})

    guarded = _pin_through(
        monkeypatch,
        handler,
        {"www.example.test": "93.184.216.34", "internal.example.test": "10.0.0.5"},
    )
    with pytest.raises(ConnectorError):
        await f.safe_fetch("https://www.example.test/")
    assert guarded[-1] == "http://internal.example.test/admin"
    assert reached == ["www.example.test"]


async def test_a_network_failure_is_a_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TLS or connect failure must surface as ConnectorError so a website
    sync skips the page instead of aborting."""
    import oc8.knowledge.connectors.fetcher as f

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]", request=request)

    _pin_through(monkeypatch, handler, {"www.example.test": "93.184.216.34"})
    with pytest.raises(ConnectorError):
        await f.safe_fetch("https://www.example.test/")
    with pytest.raises(ConnectorError):
        await f.safe_fetch_bytes("https://www.example.test/")


async def test_website_sync_skips_a_page_that_fails_to_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oc8.knowledge.connectors import website

    async def fake_fetch(url: str) -> tuple[str, str]:
        if url.endswith("/broken"):
            raise httpx.ConnectError("boom")
        return '<a href="/broken">x</a><a href="/ok">y</a>', "text/html"

    monkeypatch.setattr(website, "safe_fetch", fake_fetch)
    docs = [
        d
        async for d in website.WebsiteConnector().fetch(
            {"url": "https://www.example.test/", "maxPages": 5}, None
        )
    ]
    assert [d.source_uri for d in docs] == [
        "https://www.example.test/",
        "https://www.example.test/ok",
    ]


async def test_fetches_send_the_oc8_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    # httpx's default "python-httpx/x" UA is what bot protection blocks; both
    # fetch paths must announce oc8 and its release version instead.
    import oc8
    import oc8.knowledge.connectors.fetcher as f

    monkeypatch.setattr(f, "_guard", lambda url: None)
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent"))
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    await f.safe_fetch("http://example.com/page")
    await f.safe_fetch_bytes("http://example.com/archive")
    assert seen == [f"oc8/{oc8.__version__}"] * 2
