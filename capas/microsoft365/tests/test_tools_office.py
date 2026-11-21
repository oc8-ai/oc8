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
from mcp_bridge.tools.office import CALL_HANDLERS, TOOLS  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()

pytestmark = pytest.mark.asyncio


def test_declares_the_word_and_excel_tools() -> None:
    names = {t.name for t in TOOLS}
    assert names == {
        "word_get_text",
        "word_replace_text",
        "excel_read_range",
        "excel_write_range",
        "excel_append_row",
        "excel_list_worksheets",
    }
    assert set(CALL_HANDLERS) == names


def _docx_bytes(paragraphs: list[str]) -> bytes:
    import io

    import docx

    doc = docx.Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def test_word_get_text_extracts_paragraphs_not_raw_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `.docx` is a zip archive: decoding `/content` as text returns the
    archive's own bytes (`PK\\x03\\x04…`, ~28 % readable), not prose. Graph has
    no server-side text export for Word, so the tool must parse locally with
    python-docx -- the same shape `powerpoint_get_text` already uses. Review
    finding N2."""
    raw = _docx_bytes(["Angebot fuer Contoso", "Lieferung bis Monatsende."])

    async def fake_get_bytes(path: str) -> bytes:
        assert path == "/drives/d1/items/f1/content"
        return raw

    monkeypatch.setattr(graph, "get_bytes", fake_get_bytes, raising=False)
    result = await CALL_HANDLERS["word_get_text"]({"driveId": "d1", "itemId": "f1"})

    assert result == "Angebot fuer Contoso\nLieferung bis Monatsende."
    assert not result.startswith("PK")
    assert "[Content_Types].xml" not in result


async def test_word_get_text_refuses_an_oversized_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The download cap lives in `graph.get_bytes` (`_MAX_BYTES`, the same
    5 MB the knowledge-base connector applies), so a huge SharePoint document
    cannot flood a run's context."""
    import httpx

    resp = httpx.Response(200, content=b"x" * (graph._MAX_BYTES + 1))
    with pytest.raises(RuntimeError) as exc:
        graph._raise_if_too_large(resp, "GET /drives/d1/items/f1/content")
    assert "too large" in str(exc.value)


async def test_excel_read_range_calls_the_workbook_api(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        captured["path"] = path
        return {"values": [["a", "b"], ["1", "2"]]}

    monkeypatch.setattr(graph, "get", fake_get)
    result = await CALL_HANDLERS["excel_read_range"](
        {"driveId": "d1", "itemId": "f1", "worksheet": "Sheet1", "range": "A1:B2"}
    )
    expected_path = "/drives/d1/items/f1/workbook/worksheets/Sheet1/range(address='A1:B2')"
    assert captured["path"] == expected_path
    assert result == [["a", "b"], ["1", "2"]]


async def test_excel_write_range_patches_values(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_patch(path: str, json: dict[str, Any]) -> dict[str, Any]:
        captured["path"] = path
        captured["json"] = json
        return {}

    monkeypatch.setattr(graph, "patch", fake_patch)
    await CALL_HANDLERS["excel_write_range"](
        {
            "driveId": "d1",
            "itemId": "f1",
            "worksheet": "Sheet1",
            "range": "A1:B1",
            "values": [["x", "y"]],
        }
    )
    expected_path = "/drives/d1/items/f1/workbook/worksheets/Sheet1/range(address='A1:B1')"
    assert captured["path"] == expected_path
    assert captured["json"]["values"] == [["x", "y"]]
