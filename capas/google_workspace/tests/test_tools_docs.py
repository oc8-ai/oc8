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
    """
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


pytestmark = pytest.mark.asyncio

# A trimmed but structurally real Docs API `documents.get` response shape --
# checked against Google's own Docs API reference (Document/Body/StructuralElement/
# Paragraph/ParagraphElement/TextRun), not assumed from a sibling API.
_DOCUMENT_PAYLOAD = {
    "documentId": "doc1",
    "body": {
        "content": [
            {"sectionBreak": {}},
            {
                "paragraph": {
                    "elements": [
                        {"textRun": {"content": "Hello "}},
                        {"textRun": {"content": "world.\n"}},
                    ]
                }
            },
            {"paragraph": {"elements": [{"textRun": {"content": "Second paragraph.\n"}}]}},
        ]
    },
}


async def test_docs_get_text_extracts_real_paragraph_prose(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import docs as tools_docs

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_DOCUMENT_PAYLOAD)

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    result = await tools_docs.docs_get_text({"documentId": "doc1"})
    assert result == "Hello world.\nSecond paragraph.\n"


async def test_docs_get_text_skips_structural_elements_that_carry_no_text() -> None:
    from mcp_bridge.tools.docs import _extract_document_text

    payload = {
        "body": {
            "content": [
                {"sectionBreak": {}},
                {"table": {"tableRows": []}},
                {"paragraph": {"elements": [{"textRun": {"content": "only this\n"}}]}},
            ]
        }
    }
    assert _extract_document_text(payload) == "only this\n"


async def test_docs_get_text_includes_text_inside_tables() -> None:
    """Whole-branch review I6. A Docs table nests real prose at
    `table.tableRows[].tableCells[].content[].paragraph` -- the same
    `StructuralElement` shape the body uses -- and a template-derived document
    (exactly what `docs_create_from_template` produces) is very often MOSTLY
    table. The walk used to skip any element without a `paragraph` key, which
    is every table, and a tool documented as extracting "all prose text" that
    silently returns a subset gives the caller no way to know content was
    dropped. Structure checked against the Docs API reference
    (Table/TableRow/TableCell/StructuralElement), including the nested table a
    real invoice-style template produces."""
    from mcp_bridge.tools.docs import _extract_document_text

    def _cell(text: str) -> dict[str, object]:
        return {"content": [{"paragraph": {"elements": [{"textRun": {"content": text}}]}}]}

    payload = {
        "body": {
            "content": [
                {"paragraph": {"elements": [{"textRun": {"content": "Invoice\n"}}]}},
                {
                    "table": {
                        "rows": 2,
                        "columns": 2,
                        "tableRows": [
                            {"tableCells": [_cell("Item\n"), _cell("Amount\n")]},
                            {
                                "tableCells": [
                                    _cell("Consulting\n"),
                                    # A table nested inside a cell: the recursion
                                    # has to be real, not one level deep.
                                    {
                                        "content": [
                                            {
                                                "table": {
                                                    "tableRows": [
                                                        {"tableCells": [_cell("1200 EUR\n")]}
                                                    ]
                                                }
                                            }
                                        ]
                                    },
                                ]
                            },
                        ],
                    }
                },
                {"paragraph": {"elements": [{"textRun": {"content": "Thank you.\n"}}]}},
            ]
        }
    }
    assert _extract_document_text(payload) == (
        "Invoice\nItem\nAmount\nConsulting\n1200 EUR\nThank you.\n"
    )


async def test_docs_replace_text_uses_batch_update_replace_all_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import docs as tools_docs

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"documentId": "doc1"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    await tools_docs.docs_replace_text(
        {"documentId": "doc1", "find": "{{name}}", "replace": "Alex"}
    )
    req = captured["requests"][0]["replaceAllText"]
    assert req["containsText"]["text"] == "{{name}}"
    assert req["replaceText"] == "Alex"


async def test_docs_create_from_template_copies_with_supports_all_drives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive API v3 requires `supportsAllDrives=true` on every files.* request
    touching a Shared Drive item, copy included -- without it Drive answers
    404 fileNotFound, which reads as a wrong ID rather than a missing param."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import docs as tools_docs

    captured_params: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/copy" in request.url.path:
            captured_params.update(dict(request.url.params))
            return httpx.Response(200, json={"id": "new-doc"})
        return httpx.Response(200, json={"documentId": "new-doc"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    result = await tools_docs.docs_create_from_template(
        {"templateDocumentId": "tpl1", "driveId": "drive-1", "name": "New Doc", "replacements": []}
    )
    assert captured_params["supportsAllDrives"] == "true"
    assert result == {"documentId": "new-doc"}
