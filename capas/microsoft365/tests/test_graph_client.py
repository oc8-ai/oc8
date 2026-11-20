"""The bridge's own tiny Graph client (`mcp_bridge/graph.py`).

It builds its `httpx.AsyncClient` itself -- no transport seam like
`oc8.oauth.http`, deliberately, since it runs in a separate process with no
oc8 imports at all. So these tests swap the class it constructs for a factory
that keeps every kwarg (`follow_redirects` above all) and only substitutes the
transport. That is what makes "does this call follow the redirect" testable
rather than assumed.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
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

sys.path.remove(str(PLUGIN_ROOT))
_evict()

pytestmark = pytest.mark.asyncio


_Handler = Callable[[httpx.Request], httpx.Response]


def _install(monkeypatch: pytest.MonkeyPatch, handler: _Handler) -> list[tuple[str, str | None]]:
    """Route graph.py's own client through `handler`, recording every hop."""
    hops: list[tuple[str, str | None]] = []

    def recording(request: httpx.Request) -> httpx.Response:
        hops.append((str(request.url), request.headers.get("authorization")))
        return handler(request)

    real = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(recording)
        return real(**kwargs)

    # `graph.httpx` IS this module's `httpx` -- patching the module object
    # directly is the same swap and does not go through a re-export mypy
    # (correctly, under no-implicit-reexport) refuses to vouch for.
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setenv("GRAPH_ACCESS_TOKEN", "graph-token")
    return hops


def _content_redirect(body: bytes) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if "graph.microsoft.com" in str(request.url):
            return httpx.Response(
                302, headers={"Location": "https://contoso.sharepoint.com/dl?t=preauth"}
            )
        return httpx.Response(200, content=body)

    return handler


async def test_get_bytes_follows_the_content_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/content` answers 302, never the bytes. Without following it,
    `resp.status_code` was neither 200 nor >= 400, so `get_bytes` returned
    `b""` and python-pptx blew up on empty input with nothing pointing at the
    cause (whole-branch review, C3)."""
    hops = _install(monkeypatch, _content_redirect(b"PK\x03\x04deck"))
    raw = await graph.get_bytes("/drives/d1/items/f1/content")
    assert raw == b"PK\x03\x04deck"
    assert len(hops) == 2, hops


async def test_get_text_follows_the_content_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same 302, on the path `files_get_content`/`word_get_text` take.
    These used to fall through `_unwrap` with an empty body and hand the model
    `{}` -- no bytes, no error, nothing to act on."""
    hops = _install(monkeypatch, _content_redirect(b"hello world"))
    assert await graph.get_text("/drives/d1/items/f1/content") == "hello world"
    assert len(hops) == 2, hops


async def test_the_graph_token_never_reaches_the_redirect_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The download URL is already pre-authenticated and lives on a different
    host; forwarding a live Graph bearer token to it buys nothing and leaks a
    credential. httpx drops `Authorization` on a cross-origin hop -- pinned
    here so an httpx upgrade cannot quietly change it."""
    hops = _install(monkeypatch, _content_redirect(b"bytes"))
    await graph.get_bytes("/drives/d1/items/f1/content")
    graph_hop, download_hop = hops
    assert graph_hop[1] == "Bearer graph-token"
    assert "sharepoint.com" in download_hop[0]
    assert download_hop[1] is None


async def test_a_403_is_worded_the_way_the_connector_words_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One vocabulary across both halves of the plugin: `_raise_for_status`
    here and `_api_error` in the connector are the same failure said the same
    way, and `_on_call_tool` now carries this text to the model verbatim."""
    _install(monkeypatch, lambda request: httpx.Response(403, text="denied"))
    with pytest.raises(RuntimeError) as exc:
        await graph.get("/users/info@contoso.com/messages")
    assert "rejected the credentials" in str(exc.value)
    assert "admin consent" in str(exc.value)


async def test_a_404_names_the_ids_rather_than_dumping_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, lambda request: httpx.Response(404, text="nope"))
    with pytest.raises(RuntimeError) as exc:
        await graph.get_bytes("/drives/d1/items/nope/content")
    assert "not found" in str(exc.value)
