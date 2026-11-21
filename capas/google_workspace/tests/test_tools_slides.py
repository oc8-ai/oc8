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

_PRESENTATION_PAYLOAD = {
    "presentationId": "p1",
    "slides": [
        {
            "objectId": "slide1",
            "pageElements": [
                {
                    "objectId": "shape1",
                    "shape": {
                        "text": {
                            "textElements": [
                                {"textRun": {"content": "Title slide\n"}},
                            ]
                        }
                    },
                }
            ],
        },
        {
            "objectId": "slide2",
            "pageElements": [
                {
                    "objectId": "shape2",
                    "shape": {
                        "text": {"textElements": [{"textRun": {"content": "Second slide body\n"}}]}
                    },
                }
            ],
        },
    ],
}


async def test_slides_get_text_joins_every_shapes_text_across_all_slides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import slides as tools_slides

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_PRESENTATION_PAYLOAD)

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    result = await tools_slides.slides_get_text({"presentationId": "p1"})
    assert result == "Title slide\nSecond slide body\n"


async def test_slides_replace_placeholder_text_deletes_then_inserts_when_the_shape_has_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import slides as tools_slides

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "GET":
            return httpx.Response(200, json=_PRESENTATION_PAYLOAD)  # shape1 has existing text
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"presentationId": "p1"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    await tools_slides.slides_replace_placeholder_text(
        {"presentationId": "p1", "objectId": "shape1", "text": "New title"}
    )
    req = captured["requests"][0]
    assert req["deleteText"]["objectId"] == "shape1"
    assert captured["requests"][1]["insertText"]["objectId"] == "shape1"
    assert captured["requests"][1]["insertText"]["text"] == "New title"


async def test_slides_replace_placeholder_text_skips_delete_for_an_empty_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the Slides API 400s on `deleteText` against a shape with no
    existing text -- this must not issue that request for a target shape the
    presentation shows as empty."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import slides as tools_slides

    empty_shape_payload = {
        "presentationId": "p1",
        "slides": [
            {
                "objectId": "slide1",
                "pageElements": [
                    {"objectId": "empty-shape", "shape": {"text": {"textElements": []}}}
                ],
            }
        ],
    }
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "GET":
            return httpx.Response(200, json=empty_shape_payload)
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"presentationId": "p1"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    await tools_slides.slides_replace_placeholder_text(
        {"presentationId": "p1", "objectId": "empty-shape", "text": "First text"}
    )
    assert len(captured["requests"]) == 1
    assert "insertText" in captured["requests"][0]


async def test_slides_create_from_template_copies_with_supports_all_drives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same Drive v3 requirement as Docs' own create_from_template: the copy
    request must carry `supportsAllDrives=true` for a Shared Drive item."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import slides as tools_slides

    captured_params: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/copy" in request.url.path:
            captured_params.update(dict(request.url.params))
            return httpx.Response(200, json={"id": "new-deck"})
        return httpx.Response(200, json={"presentationId": "new-deck"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    result = await tools_slides.slides_create_from_template(
        {
            "templatePresentationId": "tpl1",
            "driveId": "drive-1",
            "name": "New Deck",
            "replacements": [],
        }
    )
    assert captured_params["supportsAllDrives"] == "true"
    assert result == {"presentationId": "new-deck"}


async def test_slides_get_text_includes_tables_and_grouped_shapes() -> None:
    """Whole-branch review I6, the Slides half. `PageElement` is a union and
    only the `shape` arm used to be read, so text inside a slide TABLE
    (`table.tableRows[].tableCells[].text`) and inside an `elementGroup` (what
    the Slides editor produces the moment someone selects two shapes and groups
    them, nesting further PageElements at `elementGroup.children`) vanished
    silently from a tool that promises "all text from every slide"."""
    from mcp_bridge.tools.slides import _extract_presentation_text

    def _shape(text: str) -> dict[str, object]:
        return {"shape": {"text": {"textElements": [{"textRun": {"content": text}}]}}}

    payload = {
        "presentationId": "p1",
        "slides": [
            {
                "pageElements": [
                    {"objectId": "title", **_shape("Quarterly review\n")},
                    {
                        "objectId": "grid",
                        "table": {
                            "rows": 2,
                            "columns": 2,
                            "tableRows": [
                                {
                                    "tableCells": [
                                        {
                                            "text": {
                                                "textElements": [
                                                    {"textRun": {"content": "Region\n"}}
                                                ]
                                            }
                                        },
                                        {
                                            "text": {
                                                "textElements": [
                                                    {"textRun": {"content": "Revenue\n"}}
                                                ]
                                            }
                                        },
                                    ]
                                },
                                {
                                    "tableCells": [
                                        {
                                            "text": {
                                                "textElements": [{"textRun": {"content": "DACH\n"}}]
                                            }
                                        },
                                        {
                                            "text": {
                                                "textElements": [{"textRun": {"content": "1.2M\n"}}]
                                            }
                                        },
                                    ]
                                },
                            ],
                        },
                    },
                    {
                        "objectId": "group1",
                        "elementGroup": {
                            "children": [
                                {"objectId": "grouped-a", **_shape("Grouped caption\n")},
                                {
                                    "objectId": "nested-group",
                                    "elementGroup": {
                                        "children": [
                                            {"objectId": "grouped-b", **_shape("Deeply nested\n")}
                                        ]
                                    },
                                },
                            ]
                        },
                    },
                ]
            }
        ],
    }
    assert _extract_presentation_text(payload) == (
        "Quarterly review\nRegion\nRevenue\nDACH\n1.2M\nGrouped caption\nDeeply nested\n"
    )
