"""Refusals from the guarded fetch carry a type and a code, not just a message."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

import oc8.knowledge.connectors.fetcher as f
from oc8.knowledge.connectors.base import ConnectorError
from oc8.knowledge.connectors.fetcher import (
    FetchBlocked,
    FetchError,
    FetchTimeout,
    FetchTooLarge,
    FetchUnreachable,
)

pytestmark = pytest.mark.asyncio

_FETCHES = [f.safe_fetch, f.safe_fetch_bytes]


def _serve(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Answer every fetch from `handler`, with a body not yet read, as a real
    connection's is: the fetch reads the raw bytes to decode them in bounded
    steps, and an `httpx.Response(content=...)` arrives already consumed."""
    monkeypatch.setattr(f, "_guard", lambda url: None)
    real_async_client = httpx.AsyncClient

    def streamed(request: httpx.Request) -> httpx.Response:
        resp = handler(request)
        if not resp.is_stream_consumed:
            return resp
        return httpx.Response(
            resp.status_code, headers=resp.headers, stream=httpx.ByteStream(resp.content)
        )

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(streamed)
        return real_async_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)


@pytest.mark.parametrize(
    ("cls", "code"),
    [
        (FetchBlocked, "blocked"),
        (FetchUnreachable, "unreachable"),
        (FetchTooLarge, "too_large"),
        (FetchTimeout, "timeout"),
    ],
)
async def test_each_error_has_its_code_and_stays_a_connector_error(
    cls: type[FetchError], code: str
) -> None:
    exc = cls("nope")
    assert exc.code == code
    assert isinstance(exc, FetchError)
    assert isinstance(exc, ConnectorError)


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_a_private_address_is_blocked(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object]
) -> None:
    monkeypatch.setattr(f, "_resolve_ips", lambda host: ["127.0.0.1"])
    with pytest.raises(FetchBlocked) as exc:
        await fetch("http://internal.example.test/")  # type: ignore[misc]
    assert exc.value.code == "blocked"


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_a_non_http_scheme_is_blocked(fetch: Callable[..., object]) -> None:
    with pytest.raises(FetchBlocked):
        await fetch("file:///etc/passwd")  # type: ignore[misc]


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_a_connect_failure_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _serve(monkeypatch, handler)
    with pytest.raises(FetchUnreachable) as exc:
        await fetch("https://www.example.test/")  # type: ignore[misc]
    assert exc.value.status_code is None


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_a_timeout_is_a_timeout(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _serve(monkeypatch, handler)
    with pytest.raises(FetchTimeout):
        await fetch("https://www.example.test/")  # type: ignore[misc]


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_an_oversized_body_is_too_large(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object]
) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 3000))
    with pytest.raises(FetchTooLarge):
        await fetch("https://www.example.test/", max_bytes=1000)  # type: ignore[misc]


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_a_redirect_loop_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object]
) -> None:
    _serve(
        monkeypatch,
        lambda request: httpx.Response(302, headers={"location": "https://www.example.test/"}),
    )
    with pytest.raises(FetchUnreachable):
        await fetch("https://www.example.test/", max_redirects=2)  # type: ignore[misc]


async def test_an_error_status_on_bytes_is_unreachable_with_its_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(404, content=b"missing"))
    with pytest.raises(FetchUnreachable) as exc:
        await f.safe_fetch_bytes("https://www.example.test/spec.json")
    assert exc.value.status_code == 404


def _gzip_bomb(size: int) -> bytes:
    import zlib

    packer = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    return packer.compress(b"\0" * size) + packer.flush()


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_a_gzip_bomb_never_inflates_past_the_budget(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object]
) -> None:
    """64 MB of zeros gzip to ~64 KB, one chunk. Inflating it whole before
    the size check is what the bound prevents: no decompress call may hand
    back more than the remaining budget (+1 byte to notice the overflow)."""
    import zlib

    bomb = _gzip_bomb(64 * 1024 * 1024)
    assert len(bomb) < 100_000
    biggest = 0
    real = zlib.decompressobj

    class Spy:
        def __init__(self, *args: object) -> None:
            self._inner = real(*args)  # type: ignore[arg-type]

        @property
        def unconsumed_tail(self) -> bytes:
            return bytes(self._inner.unconsumed_tail)

        def decompress(self, data: bytes, max_length: int = 0) -> bytes:
            nonlocal biggest
            out = bytes(self._inner.decompress(data, max_length))
            biggest = max(biggest, len(out))
            return out

        def flush(self) -> bytes:
            return bytes(self._inner.flush())

    monkeypatch.setattr(zlib, "decompressobj", Spy)
    _serve(
        monkeypatch,
        lambda request: httpx.Response(
            200, stream=httpx.ByteStream(bomb), headers={"content-encoding": "gzip"}
        ),
    )
    with pytest.raises(FetchTooLarge):
        await fetch("https://www.example.test/", max_bytes=1_000_000)  # type: ignore[misc]
    assert 0 < biggest <= 1_000_001


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_a_gzip_body_within_the_budget_is_inflated(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object]
) -> None:
    import zlib

    packer = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    body = packer.compress(b'{"openapi": "3.1.0"}') + packer.flush()
    _serve(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            stream=httpx.ByteStream(body),
            headers={"content-encoding": "gzip", "content-type": "application/json"},
        ),
    )
    got = await fetch("https://www.example.test/")  # type: ignore[misc]
    text = got if isinstance(got, bytes) else got[0].encode()  # type: ignore[index]
    assert text == b'{"openapi": "3.1.0"}'


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_an_unsupported_encoding_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object]
) -> None:
    _serve(
        monkeypatch,
        lambda request: httpx.Response(
            200, stream=httpx.ByteStream(b"??"), headers={"content-encoding": "br"}
        ),
    )
    with pytest.raises(FetchUnreachable):
        await fetch("https://www.example.test/")  # type: ignore[misc]


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_caller_headers_are_sent_and_encodings_are_only_the_decodable_ones(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object]
) -> None:
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, content=b"{}")

    _serve(monkeypatch, handler)
    await fetch(  # type: ignore[misc]
        "https://www.example.test/",
        headers={"Accept": "application/json", "Accept-Encoding": "identity"},
    )
    await fetch("https://www.example.test/")  # type: ignore[misc]
    assert seen[0]["accept"] == "application/json"
    assert seen[0]["accept-encoding"] == "identity"
    assert seen[0]["user-agent"].startswith("oc8/")
    assert seen[1]["accept-encoding"] == "gzip, deflate"


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_caller_headers_cannot_set_the_host(fetch: Callable[..., object]) -> None:
    with pytest.raises(ValueError, match="Host"):
        await fetch("https://www.example.test/", headers={"Host": "169.254.169.254"})  # type: ignore[misc]


def _deflate(data: bytes, *, raw: bool) -> bytes:
    import zlib

    packer = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS if raw else zlib.MAX_WBITS)
    return packer.compress(data) + packer.flush()


def _encoded(body: bytes, encoding: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(
        200, stream=httpx.ByteStream(body), headers={"content-encoding": encoding}
    )


@pytest.mark.parametrize("fetch", _FETCHES)
@pytest.mark.parametrize("raw", [True, False])
async def test_a_deflate_bomb_is_too_large(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object], raw: bool
) -> None:
    _serve(monkeypatch, _encoded(_deflate(b"\0" * 32_000_000, raw=raw), "deflate"))
    with pytest.raises(FetchTooLarge):
        await fetch("https://www.example.test/", max_bytes=1_000_000)  # type: ignore[misc]


@pytest.mark.parametrize("fetch", _FETCHES)
@pytest.mark.parametrize("raw", [True, False])
async def test_a_small_deflate_body_is_inflated(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object], raw: bool
) -> None:
    _serve(monkeypatch, _encoded(_deflate(b"{}", raw=raw), "deflate"))
    got = await fetch("https://www.example.test/")  # type: ignore[misc]
    assert (got if isinstance(got, bytes) else got[0].encode()) == b"{}"  # type: ignore[index]


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_a_gzip_chain_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object]
) -> None:
    import gzip

    _serve(monkeypatch, _encoded(gzip.compress(gzip.compress(b"{}")), "gzip, gzip"))
    with pytest.raises(FetchUnreachable):
        await fetch("https://www.example.test/")  # type: ignore[misc]


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_a_multi_member_gzip_yields_the_first_member_only(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object]
) -> None:
    import gzip

    body = gzip.compress(b"{}") + gzip.compress(b"\0" * 32_000_000)
    _serve(monkeypatch, _encoded(body, "gzip"))
    got = await fetch("https://www.example.test/", max_bytes=1_000_000)  # type: ignore[misc]
    assert (got if isinstance(got, bytes) else got[0].encode()) == b"{}"  # type: ignore[index]


@pytest.mark.parametrize("fetch", _FETCHES)
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_a_truncated_compressed_body_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object], encoding: str
) -> None:
    import gzip

    whole = (
        gzip.compress(b'{"openapi": "3.1.0"}' * 50)
        if encoding == "gzip"
        else (_deflate(b'{"openapi": "3.1.0"}' * 50, raw=False))
    )
    _serve(monkeypatch, _encoded(whole[: len(whole) // 2], encoding))
    with pytest.raises(FetchUnreachable, match="ended early"):
        await fetch("https://www.example.test/")  # type: ignore[misc]


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_an_empty_body_with_an_encoding_is_empty(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object]
) -> None:
    _serve(monkeypatch, _encoded(b"", "gzip"))
    got = await fetch("https://www.example.test/")  # type: ignore[misc]
    assert (got if isinstance(got, bytes) else got[0].encode()) == b""  # type: ignore[index]


@pytest.mark.parametrize("fetch", _FETCHES)
@pytest.mark.parametrize("name", ["Authorization", "cookie", "Proxy-Authorization", "HOST"])
async def test_credential_and_host_headers_are_refused(
    fetch: Callable[..., object], name: str
) -> None:
    with pytest.raises(ValueError, match=name.lower()):
        await fetch("https://www.example.test/", headers={name: "x"})  # type: ignore[misc]


@pytest.mark.parametrize("fetch", _FETCHES)
@pytest.mark.parametrize("value", ["br", "gzip, br", "zstd", "*"])
async def test_an_accept_encoding_that_cannot_be_decoded_is_refused(
    fetch: Callable[..., object], value: str
) -> None:
    with pytest.raises(ValueError, match="Accept-Encoding"):
        await fetch("https://www.example.test/", headers={"Accept-Encoding": value})  # type: ignore[misc]


@pytest.mark.parametrize("fetch", _FETCHES)
@pytest.mark.parametrize("value", ["gzip", "deflate", "identity", "GZIP, deflate;q=0.5"])
async def test_a_decodable_accept_encoding_is_sent(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object], value: str
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["accept-encoding"])
        return httpx.Response(200, content=b"{}")

    _serve(monkeypatch, handler)
    await fetch("https://www.example.test/", headers={"Accept-Encoding": value})  # type: ignore[misc]
    assert seen == [value]


@pytest.mark.parametrize("fetch", _FETCHES)
async def test_a_cross_origin_redirect_drops_caller_headers(
    monkeypatch: pytest.MonkeyPatch, fetch: Callable[..., object]
) -> None:
    """Redirects are followed by hand, so httpx's own stripping never runs:
    beyond the first origin only the content-negotiation headers go along,
    and a later hop back to the first origin does not get the rest again."""
    seen: list[tuple[str, httpx.Headers]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers))
        hops = {
            "https://api.example.test/spec": "/spec2",
            "https://api.example.test/spec2": "https://cdn.example.test/spec",
            "https://cdn.example.test/spec": "https://api.example.test/spec3",
        }
        target = hops.get(str(request.url))
        if target:
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, content=b"{}")

    _serve(monkeypatch, handler)
    await fetch(  # type: ignore[misc]
        "https://api.example.test/spec",
        headers={
            "Accept": "application/json",
            "Accept-Language": "de",
            "Accept-Encoding": "gzip",
            "X-Api-Key": "k-secret",
        },
    )
    urls = [url for url, _ in seen]
    assert urls == [
        "https://api.example.test/spec",
        "https://api.example.test/spec2",
        "https://cdn.example.test/spec",
        "https://api.example.test/spec3",
    ]
    for _url, headers in seen[:2]:
        assert headers["x-api-key"] == "k-secret"
    for _url, headers in seen[2:]:
        assert "x-api-key" not in headers
        assert headers["accept"] == "application/json"
        assert headers["accept-language"] == "de"
        assert headers["accept-encoding"] == "gzip"
        assert headers["user-agent"].startswith("oc8/")
