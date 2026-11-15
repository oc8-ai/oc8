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
    monkeypatch.setattr(f, "_guard", lambda url: None)
    real_async_client = httpx.AsyncClient

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
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
