from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    """Drop every cached `connector`/`mcp_bridge` module from sys.modules.

    Both names are generic -- after the package restructure every plugin ships
    a package called one of them (design §5.2) -- and sys.modules is keyed by
    NAME, not by path. Called SYMMETRICALLY around the import below: before,
    so a sibling plugin's cached copy cannot answer it; after, so nothing
    generic is left cached for anyone else. The "after" half is the
    load-bearing one -- `loader.import_entry_point` (Task 6's collision fix)
    only evicts modules IT ITSELF introduced, so a name left cached here makes
    a later `find_plugin`/`load_plugin` for gdrive_source or google_workspace
    silently hand back THIS plugin's code. Verified live. The module objects
    bound by the import block stay valid across the eviction.
    """
    for _stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge", "events"}
        or n.startswith(("connector.", "mcp_bridge.", "events."))
    ]:
        del sys.modules[_stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

from mcp_bridge import graph  # noqa: E402
from mcp_bridge._user import DEFAULT_USER_ENV  # noqa: E402
from mcp_bridge.tools.teams import CALL_HANDLERS, TOOLS  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()

#: The three user-scoped tools. The channel listing tool addresses a team id
#: and was never `/me`-based, so it takes no userId.
_USER_SCOPED = {"teams_list_chats", "contacts_search", "contacts_get"}

pytestmark = pytest.mark.asyncio


def test_declares_the_four_teams_and_contacts_tools() -> None:
    names = {t.name for t in TOOLS}
    assert names == {
        "teams_list_channels",
        "teams_list_chats",
        "contacts_search",
        "contacts_get",
    }
    assert set(CALL_HANDLERS) == names


def test_ships_no_teams_send_tool() -> None:
    """Graph has no APPLICATION permission for posting a Teams channel or chat
    message -- `ChannelMessage.Send`/`Chat.ReadWrite` are delegated-only, and
    this plugin's grant is `client_credentials` (design doc §4.1b). A send tool
    here could only ever 403, so it must not exist: a tool that is present but
    permanently broken is worse than an absent one, because the model will keep
    trying it. Removed in final-review N1."""
    names = {t.name for t in TOOLS} | set(CALL_HANDLERS)
    assert "teams_post_channel_message" not in names
    assert "teams_send_chat_message" not in names
    assert not any("send" in n or "post" in n for n in names), names


def test_only_the_user_scoped_tools_take_a_user_id() -> None:
    for tool in TOOLS:
        has_user_id = "userId" in tool.input_schema["properties"]
        assert has_user_id is (tool.name in _USER_SCOPED), tool.name
        assert "userId" not in tool.input_schema.get("required", []), tool.name


async def test_list_chats_addresses_the_named_user_not_me(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/me/chats` is a 400 under app-only auth -- review C1."""
    captured: dict[str, Any] = {}

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        captured["path"] = path
        return {"value": [{"id": "chat1"}]}

    monkeypatch.setattr(graph, "get", fake_get)
    monkeypatch.delenv(DEFAULT_USER_ENV, raising=False)
    result = await CALL_HANDLERS["teams_list_chats"]({"userId": "sina@contoso.com"})
    assert captured["path"] == "/users/sina@contoso.com/chats"
    assert result == [{"id": "chat1"}]


async def test_contacts_search_falls_back_to_the_default_mailbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        captured["path"] = path
        captured["params"] = params
        return {"value": []}

    monkeypatch.setattr(graph, "get", fake_get)
    monkeypatch.setenv(DEFAULT_USER_ENV, "info@contoso.com")
    await CALL_HANDLERS["contacts_search"]({"query": "ada"})
    assert captured["path"] == "/users/info@contoso.com/contacts"
    assert captured["params"]["$search"] == '"ada"'


async def test_contacts_get_addresses_a_user(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        captured["path"] = path
        return {"id": "c1"}

    monkeypatch.setattr(graph, "get", fake_get)
    monkeypatch.setenv(DEFAULT_USER_ENV, "info@contoso.com")
    await CALL_HANDLERS["contacts_get"]({"contactId": "c1"})
    assert captured["path"] == "/users/info@contoso.com/contacts/c1"


async def test_a_user_scoped_call_without_a_mailbox_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DEFAULT_USER_ENV, raising=False)
    with pytest.raises(RuntimeError) as exc:
        await CALL_HANDLERS["teams_list_chats"]({})
    assert "userId" in str(exc.value)
