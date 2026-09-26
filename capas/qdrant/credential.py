"""Credential-type test for qdrant_api: GET /collections with the given key."""

from __future__ import annotations

import httpx


async def validate_qdrant(values: dict[str, str]) -> None:
    url = (values.get("url") or "").rstrip("/")
    if not url:
        raise ValueError("url is required")
    headers: dict[str, str] = {}
    api_key = values.get("api_key") or ""
    if api_key:
        headers["api-key"] = api_key
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{url}/collections", headers=headers)
        if resp.status_code >= 400:
            raise ValueError(f"Qdrant returned HTTP {resp.status_code}: {resp.text[:200]}")
