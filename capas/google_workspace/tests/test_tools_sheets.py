from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


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
    """
    for _stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge", "events"}
        or n.startswith(("connector.", "mcp_bridge.", "events."))
    ]:
        del sys.modules[_stale]


@pytest.fixture(autouse=True)
def _plugin_path() -> Iterator[None]:
    """Make THIS plugin's packages the ones that resolve, for each test.

    Function-scoped and autouse, per Task 7's convention: every plugin import
    in this file sits INSIDE a test body and resolves at execution time, long
    after any collection-time module-top prelude would have run.

    Before the move this file had NO path handling at all -- it worked only
    because a sibling test module in the same directory had already inserted
    the plugin root at collection time and left it there. That is exactly the
    accident this fixture replaces: each file now makes its own plugin
    resolvable, and leaves nothing behind for anyone else.
    """
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


pytestmark = pytest.mark.asyncio


async def test_sheets_read_range_returns_the_values_grid_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import sheets as tools_sheets

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"range": "Sheet1!A1:B2", "values": [["a", "b"], ["c", "d"]]}
        )

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    result = await tools_sheets.sheets_read_range({"spreadsheetId": "s1", "range": "Sheet1!A1:B2"})
    assert result["values"] == [["a", "b"], ["c", "d"]]


async def test_sheets_write_range_puts_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import sheets as tools_sheets

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"updatedCells": 4})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    await tools_sheets.sheets_write_range(
        {"spreadsheetId": "s1", "range": "Sheet1!A1:B2", "values": [["x", "y"]]}
    )
    assert captured["method"] == "PUT"
    assert captured["body"]["values"] == [["x", "y"]]


async def test_sheets_append_row_posts_to_the_append_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import sheets as tools_sheets

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"updates": {"updatedRows": 1}})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    result = await tools_sheets.sheets_append_row(
        {"spreadsheetId": "s1", "range": "Sheet1!A1", "row": ["x", "y"]}
    )
    assert ":append" in str(captured["url"])
    assert result["updates"]["updatedRows"] == 1


async def test_sheets_list_worksheets_returns_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import sheets as tools_sheets

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "sheets": [
                    {"properties": {"sheetId": 0, "title": "Sheet1"}},
                    {"properties": {"sheetId": 1, "title": "Data"}},
                ]
            },
        )

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    result = await tools_sheets.sheets_list_worksheets({"spreadsheetId": "s1"})
    assert result == ["Sheet1", "Data"]
