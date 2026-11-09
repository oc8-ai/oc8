from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    names = {"connector", "mcp_bridge"}
    prefixes = ("connector.", "mcp_bridge.")
    for stale in [n for n in sys.modules if n in names or n.startswith(prefixes)]:
        del sys.modules[stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))
from mcp_bridge import graph  # noqa: E402
from mcp_bridge._user import DEFAULT_USER_ENV, user_base  # noqa: E402
from mcp_bridge.tools import mail  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()


@pytest.fixture
def personal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OC8_OAUTH_GRANT", "authorization_code")
    monkeypatch.setenv("OC8_OAUTH_ACCOUNT", "Anna@Contoso.com")
    monkeypatch.setenv(DEFAULT_USER_ENV, "info@contoso.com")  # an org default never applies here


def test_a_personal_token_works_on_me(personal: None) -> None:
    assert user_base({}) == "/me"
    assert user_base({"userId": "anna@contoso.com"}) == "/me"  # her own id is still her own mailbox


def test_a_personal_token_refuses_another_mailbox(personal: None) -> None:
    with pytest.raises(RuntimeError, match=r"Anna@Contoso\.com only"):
        user_base({"userId": "shared@contoso.com"})


def test_an_app_token_keeps_naming_the_mailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OC8_OAUTH_GRANT", raising=False)
    monkeypatch.setenv(DEFAULT_USER_ENV, "info@contoso.com")
    assert user_base({}) == "/users/info@contoso.com"
    assert user_base({"userId": "x@contoso.com"}) == "/users/x@contoso.com"


@pytest.mark.asyncio
async def test_mail_search_reads_the_persons_own_mailbox(
    personal: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_get(path: str, params: dict[str, str] | None = None) -> dict[str, object]:
        seen["path"] = path
        return {"value": []}

    monkeypatch.setattr(graph, "get", fake_get)
    await mail.mail_search({"query": "invoice"})
    assert seen["path"] == "/me/messages"


@pytest.mark.asyncio
async def test_mail_search_cannot_reach_another_mailbox(
    personal: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get(path: str, params: dict[str, str] | None = None) -> dict[str, object]:
        raise AssertionError(f"must not call Graph: {path}")

    monkeypatch.setattr(graph, "get", fake_get)
    with pytest.raises(RuntimeError):
        await mail.mail_search({"query": "x", "userId": "boss@contoso.com"})
