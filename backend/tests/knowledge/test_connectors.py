from __future__ import annotations

import pytest

from oc8.knowledge.connectors.base import ConnectorError
from oc8.knowledge.connectors.registry import get_connector

pytestmark = pytest.mark.asyncio


async def test_upload_yields_one_doc() -> None:
    c = get_connector("upload")
    cfg = {"filename": "a.md", "content": "# Hi", "content_type": "text/markdown"}
    docs = [d async for d in c.fetch(cfg, None)]
    assert len(docs) == 1
    assert docs[0].content == "# Hi" and docs[0].content_type == "text/markdown"
    assert docs[0].content_hash  # non-empty


async def test_website_crawls_and_honors_max_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    import oc8.knowledge.connectors.website as w

    pages = {
        "https://ex.com/": (
            '<a href="/b">b</a><a href="https://other.com/x">x</a>hello',
            "text/html",
        ),
        "https://ex.com/b": ("world", "text/html"),
    }

    async def fake_fetch(url: str, **kw: object) -> tuple[str, str]:
        return pages[url]

    monkeypatch.setattr(w, "safe_fetch", fake_fetch)
    c = get_connector("website")
    cfg = {"url": "https://ex.com/", "maxPages": 2, "sameHostOnly": True}
    docs = [d async for d in c.fetch(cfg, None)]
    uris = {d.source_uri for d in docs}
    assert "https://ex.com/" in uris and "https://ex.com/b" in uris
    assert "https://other.com/x" not in uris  # same-host only
    assert len(docs) <= 2


async def test_website_skips_cursor_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    import oc8.knowledge.connectors.website as w

    async def fake_fetch(url: str, **kw: object) -> tuple[str, str]:
        return ("stable", "text/html")

    monkeypatch.setattr(w, "safe_fetch", fake_fetch)
    c = get_connector("website")
    first = [d async for d in c.fetch({"url": "https://ex.com/", "maxPages": 1}, None)]
    cursor = {"hashes": [first[0].content_hash]}
    second = [d async for d in c.fetch({"url": "https://ex.com/", "maxPages": 1}, cursor)]
    assert second == []  # unchanged content skipped


async def test_website_bounds_fetches_not_emits_on_incremental_recrawl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """maxPages must bound pages FETCHED, not pages emitted.

    Regression test: a start page links to many distinct pages, and every
    page's content is already present in the cursor's hash set (so nothing
    is ever emitted). Before the fix, `emitted` never incremented on a
    cursor-skip, so the crawl loop kept fetching every reachable distinct
    URL regardless of maxPages -- unbounded on sites with crawler traps.
    """
    import hashlib

    import oc8.knowledge.connectors.website as w

    start_html = "".join(f'<a href="/p{i}">p{i}</a>' for i in range(10))
    stable_content = "stable"
    call_count = 0

    async def fake_fetch(url: str, **kw: object) -> tuple[str, str]:
        nonlocal call_count
        call_count += 1
        if url == "https://ex.com/":
            return start_html, "text/html"
        return stable_content, "text/html"

    monkeypatch.setattr(w, "safe_fetch", fake_fetch)
    c = get_connector("website")
    cursor = {
        "hashes": [
            hashlib.sha256(start_html.encode()).hexdigest(),
            hashlib.sha256(stable_content.encode()).hexdigest(),
        ]
    }
    cfg = {"url": "https://ex.com/", "maxPages": 3, "sameHostOnly": True}
    docs = [d async for d in c.fetch(cfg, cursor)]
    assert docs == []  # every page's content_hash was already in the cursor
    assert call_count <= 3  # bounded on pages fetched, not pages emitted


async def test_unknown_connector_raises() -> None:
    with pytest.raises(ConnectorError):
        get_connector("nope")


async def test_website_fails_in_words_when_the_start_page_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start page that cannot be fetched used to end the crawl with nothing
    and no error, so the sync "succeeded" with 0 documents and nobody knew why."""
    import oc8.knowledge.connectors.website as w

    async def fake_fetch(url: str, **kw: object) -> tuple[str, str]:
        raise ConnectorError("HTTP 403")

    monkeypatch.setattr(w, "safe_fetch", fake_fetch)
    c = get_connector("website")
    with pytest.raises(ConnectorError, match=r"https://ex\.com/.*HTTP 403"):
        _ = [d async for d in c.fetch({"url": "https://ex.com/", "maxPages": 3}, None)]


async def test_website_still_skips_an_unreachable_page_after_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import oc8.knowledge.connectors.website as w

    async def fake_fetch(url: str, **kw: object) -> tuple[str, str]:
        if url.endswith("/gone"):
            raise ConnectorError("HTTP 404")
        return ('<a href="/gone">x</a>home', "text/html")

    monkeypatch.setattr(w, "safe_fetch", fake_fetch)
    c = get_connector("website")
    docs = [d async for d in c.fetch({"url": "https://ex.com/", "maxPages": 3}, None)]
    assert [d.source_uri for d in docs] == ["https://ex.com/"]
