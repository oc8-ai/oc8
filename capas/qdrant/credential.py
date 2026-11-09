"""Credential-type test for qdrant_api: GET /collections with the given key."""

from __future__ import annotations

import asyncio

import httpx

from oc8.credentials.errors import CredentialCheckFailed
from oc8.net.outbound import OutboundBlocked, PinnedTransport, guard_outbound_url


async def validate_qdrant(values: dict[str, str]) -> None:
    url = (values.get("url") or "").rstrip("/")
    if not url:
        raise CredentialCheckFailed("url is required", reason="invalid")
    headers: dict[str, str] = {}
    api_key = values.get("api_key") or ""
    if api_key:
        headers["api-key"] = api_key
    # Resolve once, refuse internal addresses, then connect to exactly that
    # address (DNS rebinding). Nothing from the response is put in the message.
    try:
        ip = await asyncio.to_thread(guard_outbound_url, url)
    except OutboundBlocked as exc:
        raise CredentialCheckFailed(
            "The Qdrant address is not reachable from here", reason="unreachable"
        ) from exc
    target = url if "://" in url else f"https://{url}"
    async with httpx.AsyncClient(
        timeout=10.0, transport=PinnedTransport(ip), follow_redirects=False
    ) as client:
        resp = await client.get(f"{target}/collections", headers=headers)
    if resp.status_code >= 300:
        raise CredentialCheckFailed(
            f"Qdrant answered with HTTP {resp.status_code}",
            reason="auth" if resp.status_code in (401, 403) else "invalid",
        )
