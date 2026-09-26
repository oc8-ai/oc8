"""Qdrant query-only vector index."""

from __future__ import annotations

from typing import Any

import httpx

from oc8.knowledge.vector_indexes.base import VectorHit, VectorIndexError


def _payload_text(payload: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _payload_uri(payload: dict[str, Any], keys: list[str], fallback: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return fallback


class QdrantVectorIndex:
    type_id = "qdrant"
    label = "Qdrant"
    description = "Search an existing Qdrant collection (query-only, no upsert)."
    credential_type = "qdrant_api"
    config_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "collection": {
                "type": "string",
                "title": "Collection",
                "description": "Name of the existing Qdrant collection to search.",
            },
            "contentFields": {
                "type": "string",
                "title": "Content payload fields",
                "description": "Comma-separated payload keys tried in order for text.",
                "default": "text,document,content,page_content",
            },
            "uriFields": {
                "type": "string",
                "title": "Source URI payload fields",
                "description": "Comma-separated payload keys tried in order for source URI.",
                "default": "source,uri,url,source_uri",
            },
            "classificationField": {
                "type": "string",
                "title": "Classification payload field",
                "description": "Optional payload key whose value overrides the KB classification.",
                "default": "",
            },
        },
        "required": ["collection"],
    }

    async def _client_creds(self, auth: Any, credential_id: str) -> tuple[str, dict[str, str]]:
        url = (await auth.credential(credential_id, "url")).rstrip("/")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        try:
            api_key = await auth.credential(credential_id, "api_key")
        except Exception:
            api_key = ""
        if api_key:
            headers["api-key"] = api_key
        return url, headers

    def _field_list(self, config: dict[str, Any], key: str, default: str) -> list[str]:
        raw = str(config.get(key) or default)
        return [p.strip() for p in raw.split(",") if p.strip()]

    async def validate(
        self,
        config: dict[str, Any],
        auth: Any,
        *,
        credential_id: str,
    ) -> None:
        collection = str(config.get("collection") or "").strip()
        if not collection:
            raise VectorIndexError("collection is required")
        url, headers = await self._client_creds(auth, credential_id)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{url}/collections/{collection}", headers=headers)
            if resp.status_code == 404:
                raise VectorIndexError(f"Qdrant collection {collection!r} not found")
            if resp.status_code >= 400:
                raise VectorIndexError(
                    f"Qdrant probe failed (HTTP {resp.status_code}): {resp.text[:200]}"
                )

    async def search(
        self,
        config: dict[str, Any],
        auth: Any,
        *,
        credential_id: str,
        query_embedding: list[float],
        query_text: str,
        limit: int,
    ) -> list[VectorHit]:
        del query_text  # embedding-only search in v1
        collection = str(config.get("collection") or "").strip()
        if not collection:
            raise VectorIndexError("collection is required")
        content_keys = self._field_list(
            config, "contentFields", "text,document,content,page_content"
        )
        uri_keys = self._field_list(config, "uriFields", "source,uri,url,source_uri")
        class_field = str(config.get("classificationField") or "").strip()
        url, headers = await self._client_creds(auth, credential_id)
        body_query = {
            "query": query_embedding,
            "limit": limit,
            "with_payload": True,
        }
        body_search = {
            "vector": query_embedding,
            "limit": limit,
            "with_payload": True,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{url}/collections/{collection}/points/query",
                headers=headers,
                json=body_query,
            )
            # Older Qdrant: fall back to /points/search
            if resp.status_code in (404, 400):
                resp = await client.post(
                    f"{url}/collections/{collection}/points/search",
                    headers=headers,
                    json=body_search,
                )
            if resp.status_code >= 400:
                raise VectorIndexError(
                    f"Qdrant search failed (HTTP {resp.status_code}): {resp.text[:200]}"
                )
            data = resp.json()
        points = data.get("result", data) if isinstance(data, dict) else []
        if isinstance(points, dict):
            points = points.get("points", [])
        hits: list[VectorHit] = []
        for point in points or []:
            if not isinstance(point, dict):
                continue
            payload = point.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            content = _payload_text(payload, content_keys)
            if not content:
                continue
            point_id = point.get("id", "")
            uri = _payload_uri(payload, uri_keys, f"qdrant://{collection}/{point_id}")
            classification = None
            if class_field and isinstance(payload.get(class_field), str):
                classification = payload[class_field]
            score = float(point.get("score") or 0.0)
            hits.append(
                VectorHit(
                    content=content,
                    source_uri=uri,
                    score=score,
                    classification=classification,
                    metadata=dict(payload),
                )
            )
        return hits


def register(contrib: Any) -> None:
    contrib.add_vector_index(QdrantVectorIndex())
