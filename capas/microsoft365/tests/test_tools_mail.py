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
from mcp_bridge.tools.mail import CALL_HANDLERS, TOOLS  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()

pytestmark = pytest.mark.asyncio


def test_declares_the_eleven_mail_tools() -> None:
    names = {t.name for t in TOOLS}
    assert names == {
        "mail_search",
        "mail_get",
        "mail_send",
        "mail_reply",
        "mail_create_draft",
        "mail_move",
        "mail_delete",
        "mail_list_folders",
        "mail_mark_read",
        "mail_list_attachments",
        "mail_get_attachment",
    }
    assert set(CALL_HANDLERS) == names


def test_every_mail_tool_accepts_an_optional_user_id() -> None:
    """`/me` does not exist under app-only auth, so each tool must be able to
    say WHICH mailbox -- and must not force the model to, since most tenants
    configure one default mailbox and never think about it again."""
    for tool in TOOLS:
        assert "userId" in tool.input_schema["properties"], tool.name
        assert "userId" not in tool.input_schema.get("required", []), tool.name
        assert "signed-in" not in (tool.description or "")


async def test_mail_search_addresses_the_named_user_not_me(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the whole-branch review's C1: this asserted `/me/messages`,
    which Graph answers with `400 -- /me request is only valid with delegated
    authentication flow` on every single call under this plugin's app-only
    token. The mock encoded the wrong belief and then certified it."""
    captured: dict[str, Any] = {}

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        captured["path"] = path
        captured["params"] = params
        return {"value": [{"id": "m1", "subject": "Hi"}]}

    monkeypatch.setattr(graph, "get", fake_get)
    monkeypatch.delenv(DEFAULT_USER_ENV, raising=False)
    result = await CALL_HANDLERS["mail_search"]({"query": "invoice", "userId": "sina@contoso.com"})
    assert captured["path"] == "/users/sina@contoso.com/messages"
    assert captured["params"]["$search"] == '"invoice"'
    assert result == [{"id": "m1", "subject": "Hi"}]


async def test_mail_search_falls_back_to_the_configured_default_mailbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        captured["path"] = path
        return {"value": []}

    monkeypatch.setattr(graph, "get", fake_get)
    monkeypatch.setenv(DEFAULT_USER_ENV, "info@contoso.com")
    await CALL_HANDLERS["mail_search"]({"query": "invoice"})
    assert captured["path"] == "/users/info@contoso.com/messages"


async def test_a_call_with_neither_a_user_id_nor_a_default_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Better than building `/users//messages` and letting Graph answer
    something unrelated -- the message names both ways to fix it."""
    monkeypatch.delenv(DEFAULT_USER_ENV, raising=False)
    with pytest.raises(RuntimeError) as exc:
        await CALL_HANDLERS["mail_search"]({"query": "invoice"})
    assert "userId" in str(exc.value)
    assert "default" in str(exc.value)


async def test_mail_send_posts_the_expected_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_post(path: str, json: dict[str, Any]) -> dict[str, Any]:
        captured["path"] = path
        captured["json"] = json
        return {}

    monkeypatch.setattr(graph, "post", fake_post)
    monkeypatch.setenv(DEFAULT_USER_ENV, "info@contoso.com")
    await CALL_HANDLERS["mail_send"]({"to": ["a@example.com"], "subject": "Hello", "body": "World"})
    assert captured["path"] == "/users/info@contoso.com/sendMail"
    recipient = captured["json"]["message"]["toRecipients"][0]["emailAddress"]["address"]
    assert recipient == "a@example.com"
    assert captured["json"]["message"]["subject"] == "Hello"


async def test_no_mail_handler_still_builds_a_me_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """All of them at once, so a new tool cannot quietly reintroduce `/me`."""
    seen: list[str] = []

    async def record(path: str, *a: object, **k: object) -> dict[str, Any]:
        seen.append(path)
        # Answers mail_get_attachment as well as the listing tools.
        return {"value": [], "@odata.type": "#microsoft.graph.fileAttachment"}

    for verb in ("get", "post", "patch", "delete"):
        monkeypatch.setattr(graph, verb, record)
    monkeypatch.setenv(DEFAULT_USER_ENV, "info@contoso.com")

    args = {
        "query": "q",
        "messageId": "m1",
        "subject": "s",
        "body": "b",
        "to": ["a@example.com"],
        "destinationFolderId": "folder1",
        "attachmentId": "a1",
    }
    for name, handler in CALL_HANDLERS.items():
        await handler(dict(args))
        assert seen, name
    assert seen
    assert not any(p.startswith("/me") for p in seen), seen
    assert all(p.startswith("/users/info@contoso.com/") for p in seen), seen
