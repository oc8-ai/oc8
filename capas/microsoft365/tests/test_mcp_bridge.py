from __future__ import annotations

import json
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
        if n in {"connector", "mcp_bridge"} or n.startswith(("connector.", "mcp_bridge."))
    ]:
        del sys.modules[_stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

# `mcp` here is the INSTALLED MCP SDK, not this plugin -- the bridge package is
# deliberately named `mcp_bridge` precisely so that putting PLUGIN_ROOT on
# sys.path (two lines up) cannot shadow it.
import mcp.types as types  # noqa: E402
from mcp_bridge import __main__ as bridge  # noqa: E402
from mcp_bridge import graph  # noqa: E402
from mcp_bridge._user import DEFAULT_USER_ENV  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()

pytestmark = pytest.mark.asyncio

# Both callbacks ignore their `ctx` argument entirely (they read nothing off
# the request context), so every call below passes None. Typed as Any because
# the SDK's signature says `ServerRequestContext`: constructing a real one
# would add a dependency on SDK internals to prove nothing.
_NO_CTX: Any = None


def _text(result: types.CallToolResult, index: int = 0) -> str:
    """The text of one content block. `content` is a union of five block types
    in the SDK; every result this bridge produces is `TextContent`, and saying
    so once here beats an unchecked `.text` at a dozen call sites."""
    block = result.content[index]
    assert isinstance(block, types.TextContent), block
    return block.text


def test_server_constructs_and_creates_initialization_options() -> None:
    # Proves the Server(...) construction with on_list_tools=/on_call_tool=
    # actually matches the installed mcp SDK's constructor signature -- this
    # is what a decorator-API mismatch (mcp 1.x vs 2.0) would break at import
    # time, before any test even runs a handler.
    opts = bridge.server.create_initialization_options()
    assert opts.server_name == "microsoft365"


async def test_on_list_tools_returns_all_aggregated_tools() -> None:
    result = await bridge._on_list_tools(_NO_CTX, None)
    assert isinstance(result, types.ListToolsResult)
    assert {t.name for t in result.tools} == {t.name for t in bridge.ALL_TOOLS}


async def test_on_call_tool_routes_to_the_named_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_get(path: str, params: dict[str, str] | None = None) -> dict[str, object]:
        captured["path"] = path
        captured["params"] = params
        return {"value": [{"id": "m1"}]}

    monkeypatch.setattr(graph, "get", fake_get)
    monkeypatch.setenv(DEFAULT_USER_ENV, "info@contoso.com")
    params = types.CallToolRequestParams(name="mail_search", arguments={"query": "x"})
    result = await bridge._on_call_tool(_NO_CTX, params)
    assert isinstance(result, types.CallToolResult)
    # `/users/{id}/...`, never `/me/...` -- see the whole-branch review's C1.
    assert captured["path"] == "/users/info@contoso.com/messages"
    assert captured["params"]["$search"] == '"x"'
    assert json.loads(_text(result)) == [{"id": "m1"}]
    assert result.is_error is False


async def test_on_call_tool_passes_an_explicit_user_id_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_get(path: str, params: dict[str, str] | None = None) -> dict[str, object]:
        captured["path"] = path
        return {"value": []}

    monkeypatch.setattr(graph, "get", fake_get)
    monkeypatch.delenv(DEFAULT_USER_ENV, raising=False)
    params = types.CallToolRequestParams(
        name="mail_search", arguments={"query": "x", "userId": "sina@contoso.com"}
    )
    result = await bridge._on_call_tool(_NO_CTX, params)
    assert result.is_error is False
    assert captured["path"] == "/users/sina@contoso.com/messages"


async def test_on_call_tool_reports_an_error_for_an_unknown_tool() -> None:
    params = types.CallToolRequestParams(name="not_a_real_tool", arguments={})
    result = await bridge._on_call_tool(_NO_CTX, params)
    assert result.is_error is True
    assert "unknown tool" in _text(result)


async def test_a_failing_handler_becomes_a_tool_error_the_model_can_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception escaping this callback is swallowed by the mcp 2.0.0 runner
    and replaced with a generic "Internal server error" on the wire -- so the
    agent never learned that Graph had answered 403 and the permission was
    never consented, which is this pack's single most likely failure. The
    design doc §4.1 promises the opposite in as many words."""

    async def boom(path: str, params: dict[str, str] | None = None) -> dict[str, object]:
        raise RuntimeError(
            "Microsoft Graph rejected the credentials (HTTP 403) — check the app "
            "registration's Graph permissions and admin consent"
        )

    monkeypatch.setattr(graph, "get", boom)
    monkeypatch.setenv(DEFAULT_USER_ENV, "info@contoso.com")
    params = types.CallToolRequestParams(name="mail_search", arguments={"query": "x"})
    result = await bridge._on_call_tool(_NO_CTX, params)

    assert result.is_error is True
    assert "HTTP 403" in _text(result)
    assert "admin consent" in _text(result)


async def test_a_missing_default_mailbox_reaches_the_model_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other exception this pack actually raises: a configuration problem
    the operator can fix, useless if it arrives as "Internal server error"."""
    monkeypatch.delenv(DEFAULT_USER_ENV, raising=False)
    params = types.CallToolRequestParams(name="mail_search", arguments={"query": "x"})
    result = await bridge._on_call_tool(_NO_CTX, params)
    assert result.is_error is True
    assert "userId" in _text(result)


async def test_bridge_output_is_json(monkeypatch: pytest.MonkeyPatch) -> None:
    # str(dict) is Python repr (single quotes, True/None): record links parse
    # the tool output as JSON, so the bridge has to emit real JSON.
    async def _handler(args: dict[str, Any]) -> object:
        return {"a": True, "b": None}

    monkeypatch.setitem(bridge.ALL_HANDLERS, "mail_search", _handler)
    params = types.CallToolRequestParams(name="mail_search", arguments={})
    result = await bridge._on_call_tool(_NO_CTX, params)
    assert json.loads(_text(result)) == {"a": True, "b": None}


async def test_a_string_result_passes_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _handler(args: dict[str, Any]) -> object:
        return 'Hello "world"\nline two'

    monkeypatch.setitem(bridge.ALL_HANDLERS, "mail_search", _handler)
    params = types.CallToolRequestParams(name="mail_search", arguments={})
    result = await bridge._on_call_tool(_NO_CTX, params)
    assert _text(result) == 'Hello "world"\nline two'


async def test_a_json_looking_string_result_is_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Document text that happens to be JSON must not be parsed as records.
    import json

    doc = '[{"id": "1", "webViewLink": "https://drive.google.com/file/d/X/view"}]'

    async def _handler(args: dict[str, Any]) -> object:
        return doc

    monkeypatch.setitem(bridge.ALL_HANDLERS, "mail_search", _handler)
    params = types.CallToolRequestParams(name="mail_search", arguments={})
    result = await bridge._on_call_tool(_NO_CTX, params)
    assert json.loads(_text(result)) == doc
