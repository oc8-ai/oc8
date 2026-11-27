"""The search providers `web_search` can ask: SearXNG, Brave and Tavily.

Each one is a single HTTP request that maps the provider's own answer to
`SearchHit`s. What every provider shares lives here once: at most
`MAX_RESULTS` hits, a `TIMEOUT_S` timeout, one retry on a network failure or
a server error, and any other answer than HTTP 200 as `WebSearchError`.

A key travels only in a request header. No error message, log line or hit
ever carries it, and no redirect is followed: an answer that points
elsewhere is an error, so a request can only ever reach the host it was
built for.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from oc8.net.outbound import USER_AGENT, OutboundBlocked, PinnedTransport, guard_outbound_url

logger = logging.getLogger(__name__)

ProviderName = Literal["searxng", "brave", "tavily"]
PROVIDERS: tuple[ProviderName, ...] = ("searxng", "brave", "tavily")
#: The providers that need an API key (SearXNG needs a base URL instead).
KEYED_PROVIDERS: frozenset[str] = frozenset({"brave", "tavily"})

#: The most hits one search returns, whatever the model asks for.
MAX_RESULTS = 8
#: Per attempt; a search is tried at most twice.
TIMEOUT_S = 10.0
#: A provider's answer larger than this is refused, not parsed.
MAX_RESPONSE_BYTES = 2_000_000

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
TAVILY_ENDPOINT = "https://api.tavily.com/search"

_MAX_TITLE = 300
_MAX_SNIPPET = 600


class WebSearchError(Exception):
    """The provider could not answer: the model is told, the run goes on."""


class WebSearchUnavailable(Exception):
    """No provider is configured for this tenant (or it switched search off)."""


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class ProviderConfig:
    """One resolved provider. `internal_host` is the operator's own SearXNG
    host (`OC8_WEB_SEARCH_BASE_URL`): the one host a search may reach on an
    internal address. A tenant's base URL never gets it."""

    provider: ProviderName
    base_url: str | None = None
    api_key: str | None = None
    internal_host: str | None = None

    def __repr__(self) -> str:  # never the key, whoever prints this
        return f"ProviderConfig(provider={self.provider!r}, base_url={self.base_url!r})"


def clamp_results(max_results: object) -> int:
    """`max_results` as a number between 1 and MAX_RESULTS (5 when unreadable)."""
    try:
        wanted = int(max_results)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        wanted = 5
    return max(1, min(MAX_RESULTS, wanted))


def _text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _hits(rows: object, *, snippet_key: str, max_results: int) -> list[SearchHit]:
    """The provider's result rows as hits: http(s) URLs only, empty rows
    skipped, at most `max_results`."""
    out: list[SearchHit] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if len(out) >= max_results:
            break
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        out.append(
            SearchHit(
                title=_text(row.get("title"), _MAX_TITLE) or url,
                url=url,
                snippet=_text(row.get(snippet_key), _MAX_SNIPPET),
            )
        )
    return out


def _transport_for(url: str, config: ProviderConfig) -> httpx.AsyncBaseTransport | None:
    """The pinned transport for a SearXNG request, after the same guard
    `fetch_url` passes through -- unless the URL's host is the operator's own
    instance, which may live on the cluster network."""
    host = (urlsplit(url).hostname or "").lower()
    if config.internal_host and host == config.internal_host.lower():
        return None
    try:
        ip = guard_outbound_url(url)
    except (OutboundBlocked, ValueError) as exc:
        raise WebSearchError(
            "the search server's address is not reachable from oc8 (private or blocked network)"
        ) from exc
    return PinnedTransport(ip)


@dataclass(frozen=True)
class _Request:
    method: str
    url: str
    headers: dict[str, str]
    params: dict[str, str] | None = None
    body: dict[str, Any] | None = None


async def _send(
    provider: str,
    request: _Request,
    transport: httpx.AsyncBaseTransport | None,
) -> Any:
    """Send `request` (once more on a network failure or a 5xx/429) and
    return its JSON body; anything but HTTP 200 is a `WebSearchError`."""
    last: str = "no answer"
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT_S, follow_redirects=False, transport=transport
            ) as client:
                async with client.stream(
                    request.method,
                    request.url,
                    params=request.params,
                    headers=request.headers,
                    json=request.body,
                ) as resp:
                    if resp.status_code != 200:
                        last = f"{provider} answered HTTP {resp.status_code}"
                        if resp.status_code >= 500 or resp.status_code == 429:
                            continue
                        raise WebSearchError(last)
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise WebSearchError(f"{provider} answered with too much data")
        except httpx.TimeoutException:
            last = f"{provider} did not answer within {TIMEOUT_S:g} seconds"
            logger.info("web search attempt %d: %s", attempt, last)
            continue
        except httpx.HTTPError as exc:
            # The class name only: an httpx message may quote the request.
            last = f"{provider} could not be reached ({type(exc).__name__})"
            logger.info("web search attempt %d: %s", attempt, last)
            continue
        try:
            return json.loads(bytes(body))
        except ValueError as exc:
            raise WebSearchError(f"{provider} answered with something that is not JSON") from exc
    raise WebSearchError(last)


def _searxng_request(config: ProviderConfig, query: str) -> _Request:
    if not config.base_url:
        raise WebSearchUnavailable("SearXNG has no base URL")
    base = config.base_url.rstrip("/")
    return _Request(
        "GET",
        f"{base}/search",
        params={"q": query, "format": "json"},
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )


def _brave_request(config: ProviderConfig, query: str, max_results: int) -> _Request:
    if not config.api_key:
        raise WebSearchUnavailable("Brave Search has no API key")
    return _Request(
        "GET",
        BRAVE_ENDPOINT,
        params={"q": query, "count": str(max_results)},
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-Subscription-Token": config.api_key,
        },
    )


def _tavily_request(config: ProviderConfig, query: str, max_results: int) -> _Request:
    if not config.api_key:
        raise WebSearchUnavailable("Tavily has no API key")
    return _Request(
        "POST",
        TAVILY_ENDPOINT,
        body={"query": query, "max_results": max_results},
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {config.api_key}",
        },
    )


async def run_search(config: ProviderConfig, query: str, max_results: int) -> list[SearchHit]:
    """Ask `config`'s provider for `query`; at most MAX_RESULTS hits."""
    wanted = clamp_results(max_results)
    if config.provider == "searxng":
        request = _searxng_request(config, query)
        body = await _send("SearXNG", request, _transport_for(request.url, config))
        rows = body.get("results") if isinstance(body, dict) else None
        return _hits(rows, snippet_key="content", max_results=wanted)
    if config.provider == "brave":
        body = await _send("Brave Search", _brave_request(config, query, wanted), None)
        web = body.get("web") if isinstance(body, dict) else None
        rows = web.get("results") if isinstance(web, dict) else None
        return _hits(rows, snippet_key="description", max_results=wanted)
    if config.provider == "tavily":
        body = await _send("Tavily", _tavily_request(config, query, wanted), None)
        rows = body.get("results") if isinstance(body, dict) else None
        return _hits(rows, snippet_key="content", max_results=wanted)
    raise WebSearchUnavailable(f"unknown search provider {config.provider!r}")
