"""Single outbound HTTP client for provider calls, with a test transport seam.

Deliberately NOT the SSRF-guarded fetcher (`knowledge/connectors/fetcher.py`):
that guard exists for user-supplied URLs and blocks private ranges, which is
irrelevant and needlessly restrictive when talking to a known provider host.
"""

from __future__ import annotations

import httpx

_TIMEOUT = 15.0
_override: httpx.AsyncBaseTransport | None = None


def set_transport_override(transport: httpx.AsyncBaseTransport | None) -> None:
    """Install (or clear) a transport for tests. Never used in production code."""
    global _override
    _override = transport


def current_override() -> httpx.AsyncBaseTransport | None:
    """The test transport, if one is installed (so a pinned transport can wrap it)."""
    return _override


def get_client(
    *, follow_redirects: bool = False, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """A client for one provider call.

    Redirects are refused by default -- a provider API answering 3xx where a
    caller expected a payload is a surprise worth surfacing, not silently
    chasing. `follow_redirects=True` is for the download endpoints that are
    *documented* to redirect: Microsoft Graph's `/drives/{id}/items/{id}/content`
    answers 302 with a `Location` on a different, pre-authenticated host, so a
    caller that does not follow it gets an empty body and no error at all.
    httpx drops the `Authorization` header on any cross-origin hop, which is
    exactly right here: the redirect target carries its own credential and must
    never see the provider bearer token.
    """
    return httpx.AsyncClient(
        transport=transport or _override, timeout=_TIMEOUT, follow_redirects=follow_redirects
    )
