"""validate_qdrant: guarded, pinned, and never echoes the response."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    for _stale in [n for n in sys.modules if n == "credential" or n.startswith("credential.")]:
        del sys.modules[_stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

from credential import validate_qdrant  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()

pytestmark = pytest.mark.asyncio

_VALUES = {"url": "https://qdrant.acme.test:6333", "api_key": "k"}


def _resolver(answers: list[list[str]]):  # type: ignore[no-untyped-def]
    it = iter(answers)
    return lambda host: next(it)


async def test_internal_urls_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("oc8.net.outbound.resolve_ips", lambda host: ["10.0.0.5"])
    with pytest.raises(ValueError, match="not reachable"):
        await validate_qdrant(_VALUES)


async def test_the_request_goes_to_the_checked_address_and_follows_no_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oc8.net.outbound import PinnedTransport

    monkeypatch.setattr(
        "oc8.net.outbound.resolve_ips", _resolver([["93.184.216.34"], ["10.0.0.5"]])
    )
    seen: dict[str, object] = {}

    async def fake_get(self: httpx.AsyncClient, url: str, **kw: object) -> httpx.Response:
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    real = httpx.AsyncClient

    def factory(*a: object, **kw: object) -> httpx.AsyncClient:
        seen.update(kw)
        return real(*a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    await validate_qdrant(_VALUES)
    assert isinstance(seen["transport"], PinnedTransport)
    assert seen["transport"]._ip == "93.184.216.34"  # type: ignore[attr-defined]
    assert seen["follow_redirects"] is False


async def test_a_failure_does_not_echo_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("oc8.net.outbound.resolve_ips", lambda host: ["93.184.216.34"])

    async def fake_get(self: httpx.AsyncClient, url: str, **kw: object) -> httpx.Response:
        return httpx.Response(
            500, text="boom sk-live-1 http://10.0.0.9", request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(ValueError) as err:
        await validate_qdrant(_VALUES)
    assert str(err.value) == "Qdrant answered with HTTP 500"
