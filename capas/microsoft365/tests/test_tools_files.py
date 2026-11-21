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
from mcp_bridge.tools.files import CALL_HANDLERS, TOOLS  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()

pytestmark = pytest.mark.asyncio


def test_declares_the_seven_files_tools() -> None:
    names = {t.name for t in TOOLS}
    assert names == {
        "files_list",
        "files_get_content",
        "files_upload",
        "files_update_content",
        "files_delete",
        "files_search",
        "sites_list",
    }
    assert set(CALL_HANDLERS) == names


async def test_files_upload_puts_content_at_the_right_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_put(path: str, content: bytes) -> dict[str, Any]:
        captured["path"] = path
        captured["content"] = content
        return {"id": "f1"}

    monkeypatch.setattr(graph, "put_bytes", fake_put, raising=False)
    result = await CALL_HANDLERS["files_upload"](
        {"driveId": "d1", "path": "reports/q1.txt", "content": "hello"}
    )
    assert captured["path"] == "/drives/d1/root:/reports/q1.txt:/content"
    assert captured["content"] == b"hello"
    assert result == {"id": "f1"}
