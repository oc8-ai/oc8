from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    """Drop every cached `connector`/`mcp_bridge` module: both names are shared
    by every capa, and sys.modules is keyed by name, not path."""
    for _stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge"} or n.startswith(("connector.", "mcp_bridge."))
    ]:
        del sys.modules[_stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

from collections.abc import Callable  # noqa: E402

import mcp.types as types  # noqa: E402
from mcp_bridge import __main__ as bridge  # noqa: E402
from mcp_bridge import client  # noqa: E402
from mcp_bridge.catalog import TOOL_FUNCTIONS  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()

_NO_CTX: Any = None


def _text(result: types.CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, types.TextContent), block
    return block.text


def test_server_constructs() -> None:
    assert bridge.server.create_initialization_options().server_name == "moodle"


def test_every_tool_has_a_json_schema_and_named_moodle_functions() -> None:
    for tool in bridge.ALL_TOOLS:
        assert tool.name.startswith("moodle_"), tool.name
        assert tool.input_schema["type"] == "object", tool.name
        assert tool.description, tool.name
        assert tool.name in TOOL_FUNCTIONS, tool.name
    assert set(bridge.ALL_HANDLERS) == {t.name for t in bridge.ALL_TOOLS}


@pytest.mark.asyncio
async def test_list_tools_returns_every_aggregated_tool() -> None:
    result = await bridge._on_list_tools(_NO_CTX, None)
    assert {t.name for t in result.tools} == {t.name for t in bridge.ALL_TOOLS}


@pytest.mark.asyncio
async def test_call_tool_returns_json(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on("core_course_get_contents", [])
    params = types.CallToolRequestParams(
        name="moodle_get_course_contents", arguments={"course_id": 5}
    )
    result = await bridge._on_call_tool(_NO_CTX, params)
    assert result.is_error is False
    assert json.loads(_text(result)) == []


@pytest.mark.asyncio
async def test_a_moodle_error_reaches_the_model_as_a_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(function: str, params: Any = None) -> Any:
        raise client.MoodleError("The Moodle token is invalid or expired.")

    monkeypatch.setattr(client, "call", refuse)
    params = types.CallToolRequestParams(name="moodle_site_info", arguments={})
    result = await bridge._on_call_tool(_NO_CTX, params)
    assert result.is_error is True
    assert "token is invalid" in _text(result)


@pytest.mark.asyncio
async def test_a_bad_argument_reaches_the_model_as_a_tool_error() -> None:
    params = types.CallToolRequestParams(
        name="moodle_search_courses", arguments={"query": "x", "id": 1}
    )
    result = await bridge._on_call_tool(_NO_CTX, params)
    assert result.is_error is True
    assert "exactly one of" in _text(result)


@pytest.mark.asyncio
async def test_unknown_tool_is_an_error() -> None:
    params = types.CallToolRequestParams(name="nope", arguments={})
    result = await bridge._on_call_tool(_NO_CTX, params)
    assert result.is_error is True
    assert "unknown tool" in _text(result)


def test_the_bridge_serves_exactly_the_catalogued_tools() -> None:
    assert {t.name for t in bridge.ALL_TOOLS} == set(TOOL_FUNCTIONS)
    assert len(bridge.ALL_TOOLS) == 19
