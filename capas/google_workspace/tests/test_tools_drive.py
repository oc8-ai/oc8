from __future__ import annotations

import sys
import uuid
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
        if n in {"connector", "mcp_bridge"} or n.startswith(("connector.", "mcp_bridge."))
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


async def test_drive_list_scopes_to_the_shared_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import drive as tools_drive

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"files": []})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    await tools_drive.drive_list({"driveId": "shared-drive-1"})
    assert captured["params"]["driveId"] == "shared-drive-1"
    assert captured["params"]["corpora"] == "drive"


async def test_drive_get_content_returns_the_bytes_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import drive as tools_drive

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"file bytes here")

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    result = await tools_drive.drive_get_content({"fileId": "f1"})
    assert result == "file bytes here"


async def test_drive_upload_puts_bytes_to_the_upload_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import drive as tools_drive

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "new-file"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    result = await tools_drive.drive_upload(
        {"driveId": "shared-drive-1", "name": "notes.txt", "content": "hello"}
    )
    assert "upload" in str(captured["url"])
    assert result == {"id": "new-file"}


async def test_drive_upload_handles_a_filename_containing_a_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the metadata part used to be built by f-string
    interpolation, so a filename containing `"` produced invalid JSON."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import drive as tools_drive

    def handler(request: httpx.Request) -> httpx.Response:
        # A malformed metadata JSON part would make Drive itself 400; the real
        # assertion here is that constructing the request doesn't raise and
        # the metadata round-trips through json.dumps correctly.
        assert (
            '\\"' in request.content.decode()
            or '"notes \\"final\\".txt"' in request.content.decode()
        )
        return httpx.Response(200, json={"id": "new-file"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    result = await tools_drive.drive_upload(
        {"driveId": "shared-drive-1", "name": 'notes "final".txt', "content": "hello"}
    )
    assert result == {"id": "new-file"}


async def test_drive_upload_uses_a_different_boundary_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the multipart boundary used to be a static, hardcoded
    string, the same on every call. A predictable boundary lets
    model-supplied `content` inject an extra MIME part (e.g. a second
    metadata part overriding the file's name/parent) by embedding the
    boundary sequence itself. Proving the boundary differs per call is the
    most direct evidence the request is no longer predictable."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import drive as tools_drive

    seen_boundaries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        content_type = request.headers["content-type"]
        boundary = content_type.split("boundary=", 1)[1]
        seen_boundaries.append(boundary)
        body = request.content.decode()
        assert body.startswith(f"--{boundary}")
        assert body.endswith(f"--{boundary}--")
        return httpx.Response(200, json={"id": "new-file"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    for _ in range(2):
        result = await tools_drive.drive_upload(
            {"driveId": "shared-drive-1", "name": "notes.txt", "content": "hello"}
        )
        assert result == {"id": "new-file"}

    assert len(seen_boundaries) == 2
    assert seen_boundaries[0] != seen_boundaries[1]


async def test_drive_upload_rejects_content_matching_a_known_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates an attacker who somehow guessed (or fixed, as with the old
    static-boundary code) the exact boundary value and crafted `content` to
    inject an extra multipart part -- e.g. a second
    `Content-Type: application/json` part that could override the real
    metadata part's name/parent after the fact. With a random boundary this
    is a near-zero-probability collision, but the defense-in-depth check
    must still catch it and refuse to build the request rather than send a
    corrupted one."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import drive as tools_drive

    # Fix the "random" boundary to a known value, standing in for an attacker
    # who has somehow learned (or, with the old code, always knew) it.
    known_boundary = uuid.UUID(int=0).hex
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=0))

    injected_content = (
        f"hello\r\n--{known_boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        '{"name": "pwned.txt", "parents": ["attacker-drive"]}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent when content collides with the boundary")

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    with pytest.raises(RuntimeError, match="boundary"):
        await tools_drive.drive_upload(
            {"driveId": "shared-drive-1", "name": "notes.txt", "content": injected_content}
        )


async def test_drive_delete_supports_shared_drive_items(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import drive as tools_drive

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200)

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    result = await tools_drive.drive_delete({"fileId": "f1"})
    assert captured["params"]["supportsAllDrives"] == "true"
    assert result == {"deleted": "f1"}


async def test_drive_list_and_search_request_the_web_view_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import drive as tools_drive

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["fields"])
        return httpx.Response(200, json={"files": []})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "self_token", lambda: "tok")

    await tools_drive.drive_list({"driveId": "d1"})
    await tools_drive.drive_search({"driveId": "d1", "query": "x"})
    assert len(seen) == 2
    assert all("webViewLink" in fields for fields in seen)
