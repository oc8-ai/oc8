from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# `mcp` here is the INSTALLED MCP SDK, not this plugin -- the bridge package is
# deliberately named `mcp_bridge` precisely so that putting PLUGIN_ROOT on
# sys.path (inside the fixture below) cannot shadow it.
import mcp.types as types
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# `_on_call_tool` ignores its `ctx` argument entirely (it reads nothing off the
# request context), so every call below passes None. Typed as Any because the
# SDK's signature says `ServerRequestContext`: constructing a real one would
# add a dependency on SDK internals to prove nothing.
_NO_CTX: Any = None


def _text(result: types.CallToolResult, index: int = 0) -> str:
    """The text of one content block. `content` is a union of five block types
    in the SDK; every result this bridge produces is `TextContent`, and saying
    so once here beats an unchecked `.text` at every call site."""
    block = result.content[index]
    assert isinstance(block, types.TextContent), block
    return block.text


def _evict() -> None:
    """Drop every cached `connector`/`mcp_bridge` module from sys.modules.

    Both names are generic -- after the package restructure every plugin ships
    a package called one of them (design §5.2) -- and sys.modules is keyed by
    NAME, not by path. Called SYMMETRICALLY on fixture setup AND teardown:
    before, so a sibling plugin's cached copy cannot answer our import; after,
    so nothing generic is left cached for anyone else. The teardown half is
    the load-bearing one -- `loader.import_entry_point` (Task 6's collision
    fix) only evicts modules IT ITSELF introduced, so a name left cached here
    makes a later `find_plugin`/`load_plugin` for gdrive_source or
    microsoft365 silently hand back THIS plugin's code. Verified live.

    Note that `mcp_bridge` is evicted, never `mcp`: the installed MCP SDK the
    bridge imports keeps resolving to the venv throughout, which is precisely
    why the bridge package is NOT called `mcp`.
    """
    for _stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge"} or n.startswith(("connector.", "mcp_bridge."))
    ]:
        del sys.modules[_stale]


@pytest.fixture(autouse=True)
def _plugin_path() -> Iterator[None]:
    """Make THIS plugin's packages the ones that resolve, for each test.

    Function-scoped and autouse, per Task 7's convention: every plugin import
    in this file sits INSIDE a test body and resolves at execution time, long
    after any collection-time module-top prelude would have run.
    """
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


pytestmark = pytest.mark.asyncio


async def test_unknown_tool_returns_an_error_result() -> None:
    from mcp_bridge.__main__ import _on_call_tool

    params = types.CallToolRequestParams(name="not_a_real_tool", arguments={})
    result = await _on_call_tool(_NO_CTX, params)
    assert result.is_error is True
    assert "unknown tool" in _text(result)


async def test_a_raising_handler_becomes_an_error_result_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import __main__ as bridge

    async def _boom(args: dict[str, object]) -> object:
        raise RuntimeError("Google API error (HTTP 403): permission denied")

    monkeypatch.setitem(bridge.ALL_HANDLERS, "gmail_search", _boom)

    params = types.CallToolRequestParams(name="gmail_search", arguments={})
    result = await bridge._on_call_tool(_NO_CTX, params)
    assert result.is_error is True
    assert "403" in _text(result)


async def test_bridge_output_is_json(monkeypatch: pytest.MonkeyPatch) -> None:
    # str(dict) is Python repr (single quotes, True/None): record links parse
    # the tool output as JSON, so the bridge has to emit real JSON.
    import json

    from mcp_bridge import __main__ as bridge

    async def _handler(args: dict[str, object]) -> object:
        return {"a": True, "b": None}

    monkeypatch.setitem(bridge.ALL_HANDLERS, "gmail_search", _handler)
    params = types.CallToolRequestParams(name="gmail_search", arguments={})
    result = await bridge._on_call_tool(_NO_CTX, params)
    assert json.loads(_text(result)) == {"a": True, "b": None}
