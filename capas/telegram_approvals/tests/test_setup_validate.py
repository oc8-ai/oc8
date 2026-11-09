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


async def test_a_missing_token_is_a_worded_check() -> None:
    from channel.setup import validate

    with pytest.raises(CredentialCheckFailed) as caught:
        await validate({})
    assert (caught.value.reason, caught.value.detail) == ("invalid", "no bot token submitted")


async def test_a_rejected_token_does_not_repeat_telegrams_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from channel.setup import validate

    _install(
        monkeypatch,
        lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized <b>x</b>"}),
    )
    with pytest.raises(CredentialCheckFailed) as caught:
        await validate({"bot_token": "123:secret"})
    assert caught.value.reason == "auth"
    assert caught.value.detail == "Telegram rejected this bot token"


async def test_an_unreachable_telegram_does_not_repeat_the_url_with_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from channel.setup import validate

    _install(monkeypatch, _unreachable)
    with pytest.raises(CredentialCheckFailed) as caught:
        await validate({"bot_token": "123:secret"})
    assert caught.value.reason == "unreachable"
    assert caught.value.detail == "Could not reach Telegram"


async def test_a_working_token_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    from channel.setup import validate

    _install(monkeypatch, lambda r: httpx.Response(200, json={"ok": True, "result": {}}))
    await validate({"bot_token": "123:secret"})
