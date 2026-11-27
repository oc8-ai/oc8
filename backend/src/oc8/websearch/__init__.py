"""Web search for the Copilot's `web_search` tool (spec §10)."""

from oc8.websearch.providers import SearchHit, WebSearchError, WebSearchUnavailable
from oc8.websearch.service import search

__all__ = ["SearchHit", "WebSearchError", "WebSearchUnavailable", "search"]
