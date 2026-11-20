"""`channel.setup.validate` answers with fixed, worded checks a person can
read (CredentialCheckFailed), never with upstream or transport text."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from oc8.credentials.errors import CredentialCheckFailed

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.asyncio


def _evict() -> None:
    for _stale in [n for n in sys.modules if n == "channel" or n.startswith("channel.")]:
        del sys.modules[_stale]


@pytest.fixture(autouse=True)
def _plugin_path() -> Iterator[None]:
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


_Handler = Callable[[httpx.Request], httpx.Response]


def _install(monkeypatch: pytest.MonkeyPatch, handler: _Handler) -> None:
    real = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)


async def test_missing_values_are_worded_checks() -> None:
    from channel.setup import validate

    with pytest.raises(CredentialCheckFailed) as caught:
        await validate({"phone_number_id": "1"})
    assert (caught.value.reason, caught.value.detail) == ("invalid", "no access token submitted")
    with pytest.raises(CredentialCheckFailed) as caught:
        await validate({"access_token": "t"})
    assert (caught.value.reason, caught.value.detail) == (
        "invalid",
        "no phone number id submitted",
    )


async def test_a_rejected_token_does_not_repeat_metas_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from channel.setup import validate

    _install(
        monkeypatch,
        lambda r: httpx.Response(401, json={"error": {"message": "Invalid OAuth token abc"}}),
    )
    with pytest.raises(CredentialCheckFailed) as caught:
        await validate({"access_token": "t", "phone_number_id": "1"})
    assert caught.value.reason == "auth"
    assert caught.value.detail == "WhatsApp rejected this token or phone number id (HTTP 401)"


async def test_an_unknown_number_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    from channel.setup import validate

    _install(monkeypatch, lambda r: httpx.Response(400, text="not json"))
    with pytest.raises(CredentialCheckFailed) as caught:
        await validate({"access_token": "t", "phone_number_id": "1"})
    assert caught.value.reason == "invalid"
    assert caught.value.detail == "WhatsApp rejected this token or phone number id (HTTP 400)"


async def test_an_unreachable_whatsapp_is_a_worded_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from channel.setup import validate

    _install(monkeypatch, _unreachable)
    with pytest.raises(CredentialCheckFailed) as caught:
        await validate({"access_token": "t", "phone_number_id": "1"})
    assert caught.value.reason == "unreachable"
    assert caught.value.detail == "Could not reach WhatsApp"


async def test_a_working_number_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    from channel.setup import validate

    _install(monkeypatch, lambda r: httpx.Response(200, json={"verified_name": "Acme"}))
    await validate({"access_token": "t", "phone_number_id": "1"})
