from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import AsyncIterator
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

from oc8.knowledge.connectors.base import (
    AuthContext,
    Connector,
    ConnectorError,
    RawDocument,
    SourceItemMeta,
    ValidationResult,
)
from oc8.knowledge.connectors.fetcher import safe_fetch


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(v)


class WebsiteConnector:
    type_id = "website"
    label = "Website"
    description = "Public web pages"
    requires_oauth: str | None = None
    config_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "maxPages": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
            "sameHostOnly": {"type": "boolean", "default": True},
        },
        "required": ["url"],
    }

    async def validate(
        self, config: dict[str, Any], auth: AuthContext | None = None
    ) -> ValidationResult:
        url = str(config.get("url", ""))
        if urlparse(url).scheme not in {"http", "https"}:
            return ValidationResult(ok=False, error="url must be http(s)")
        return ValidationResult(ok=True)

    async def discover(
        self, config: dict[str, Any], auth: AuthContext | None = None
    ) -> list[SourceItemMeta]:
        return [SourceItemMeta(uri=str(config["url"]), title=str(config["url"]))]

    async def fetch(
        self,
        config: dict[str, Any],
        cursor: dict[str, Any] | None,
        auth: AuthContext | None = None,
    ) -> AsyncIterator[RawDocument]:
        start = str(config["url"])
        max_pages = int(config.get("maxPages", 10))
        same_host = bool(config.get("sameHostOnly", True))
        start_host = urlparse(start).hostname
        seen_hashes = set((cursor or {}).get("hashes", []))
        queue: deque[str] = deque([start])
        visited: set[str] = set()
        emitted = 0
        fetched = 0
        while queue and fetched < max_pages:
            url = urldefrag(queue.popleft()).url
            if url in visited:
                continue
            visited.add(url)
            try:
                text, ctype = await safe_fetch(url)
            except (ConnectorError, httpx.HTTPError):
                # One unreachable page is skipped, not the whole sync.
                continue
            fetched += 1
            h = hashlib.sha256(text.encode()).hexdigest()
            if h not in seen_hashes:
                emitted += 1
                yield RawDocument(
                    source_uri=url,
                    title=url,
                    content=text,
                    content_type=ctype or "text/html",
                    content_hash=h,
                )
            parser = _LinkParser()
            parser.feed(text)
            for href in parser.links:
                nxt = urljoin(url, href)
                if urlparse(nxt).scheme not in {"http", "https"}:
                    continue
                if same_host and urlparse(nxt).hostname != start_host:
                    continue
                if nxt not in visited:
                    queue.append(nxt)


_WEBSITE: Connector = WebsiteConnector()
