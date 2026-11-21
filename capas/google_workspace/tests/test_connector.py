from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import httpx
import pytest

from oc8.knowledge.connectors.base import ConnectorError

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
    after any collection-time module-top prelude would have run. The real
    runtime reaches the connector through `oc8.capas.loader`, which inserts
    the plugin folder into `sys.path` itself; a test that never calls the
    loader needs the same insertion done by hand.
    """
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


pytestmark = pytest.mark.asyncio


class _FakeAuth:
    """A stand-in `AuthContext`. `secret` is never called by this connector
    (Google Workspace auth is pure OAuth/service-account) but the protocol
    requires it, and now that the plugin's own package resolves for mypy, an
    incomplete stub is a real type error rather than a silently-`Any` one."""

    async def token(self) -> str:
        return "fake-token"

    async def secret(self, ref: str) -> str:
        raise AssertionError(f"google_workspace never resolves a secret ref ({ref!r})")


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[], Awaitable[httpx.AsyncClient]]:
    async def _get_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return _get_client


async def test_type_id_is_google_workspace_files() -> None:
    from connector.connector import GoogleWorkspaceFilesConnector

    assert GoogleWorkspaceFilesConnector.type_id == "google_workspace_files"
    assert GoogleWorkspaceFilesConnector.requires_oauth == "google"


async def test_list_scopes_every_call_to_the_configured_shared_drives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from connector import connector as gw

    captured_params: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.append(dict(request.url.params))
        return httpx.Response(200, json={"files": []})

    monkeypatch.setattr(
        gw, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    files = await gw._list_files(
        {"Authorization": "Bearer x"}, ["drive-1", "drive-2"], max_files=100
    )
    assert files == []
    assert len(captured_params) == 2
    for params in captured_params:
        assert params["corpora"] == "drive"
        assert params["includeItemsFromAllDrives"] == "true"
        assert params["supportsAllDrives"] == "true"
    assert captured_params[0]["driveId"] == "drive-1"
    assert captured_params[1]["driveId"] == "drive-2"


async def test_validate_requires_at_least_one_shared_drive_id() -> None:
    from connector.connector import GoogleWorkspaceFilesConnector

    connector = GoogleWorkspaceFilesConnector()
    result = await connector.validate({"sharedDriveIds": []}, _FakeAuth())
    assert result.ok is False
    assert (
        "sharedDriveIds" in (result.error or "") or "shared drive" in (result.error or "").lower()
    )


async def test_validate_probes_each_configured_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    from connector import connector as gw

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"id": "drive-1"})

    monkeypatch.setattr(
        gw, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    connector = gw.GoogleWorkspaceFilesConnector()
    result = await connector.validate({"sharedDriveIds": ["drive-1"]}, _FakeAuth())
    assert result.ok is True
    assert any("drives/drive-1" in url for url in seen)


async def test_validate_reports_which_drive_failed_on_partial_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 configured Shared Drives, service account only added to 2 of them.

    `validate()` must fail closed (not silently pass because 2/3 drives are
    fine) AND its error must name the specific drive ID that failed -- an
    admin debugging this needs to know which of their 3 configured IDs is the
    problem, not just that "something" is wrong.
    """
    from connector import connector as gw

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/drives/drive-2"):
            return httpx.Response(403, text="The caller does not have permission")
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})

    monkeypatch.setattr(
        gw, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    connector = gw.GoogleWorkspaceFilesConnector()
    result = await connector.validate(
        {"sharedDriveIds": ["drive-1", "drive-2", "drive-3"]}, _FakeAuth()
    )
    assert result.ok is False
    assert "drive-2" in (result.error or "")
    assert "drive-1" not in (result.error or "")
    assert "drive-3" not in (result.error or "")


async def test_fetch_exports_google_native_docs_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from connector import connector as gw

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "id": "doc1",
                            "name": "Doc One",
                            "mimeType": "application/vnd.google-apps.document",
                            "modifiedTime": "2026-01-01T00:00:00Z",
                        }
                    ]
                },
            )
        if request.url.path.endswith("/export"):
            return httpx.Response(200, text="hello from docs")
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(
        gw, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    connector = gw.GoogleWorkspaceFilesConnector()
    docs = [d async for d in connector.fetch({"sharedDriveIds": ["drive-1"]}, None, _FakeAuth())]
    assert len(docs) == 1
    assert docs[0].content == "hello from docs"
    assert docs[0].source_uri == "https://drive.google.com/file/d/doc1"


async def test_fetch_raises_connector_error_without_auth() -> None:
    from connector.connector import GoogleWorkspaceFilesConnector

    connector = GoogleWorkspaceFilesConnector()
    with pytest.raises(ConnectorError):
        async for _ in connector.fetch({"sharedDriveIds": ["drive-1"]}, None, None):
            pass
