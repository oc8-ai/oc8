"""A minimal VectorIndex contributed by a plugin, used by retrieval tests."""

from __future__ import annotations

from typing import Any

from oc8.knowledge.vector_indexes.base import VectorHit, VectorIndexError


class DemoVectorIndex:
    type_id = "demo_index"
    label = "Demo index"
    description = "Fixture index that returns a fixed hit."
    credential_type = "demo_vector_cred"
    config_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "collection": {"type": "string", "title": "Collection"},
        },
        "required": ["collection"],
    }

    async def validate(
        self,
        config: dict[str, Any],
        auth: Any,
        *,
        credential_id: str,
    ) -> None:
        del auth, credential_id
        if not str(config.get("collection") or "").strip():
            raise VectorIndexError("collection is required")

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
        del auth, credential_id, query_embedding, limit
        collection = str(config.get("collection") or "demo")
        return [
            VectorHit(
                content=f"remote hit for {query_text} in {collection}",
                source_uri=f"demo-index://{collection}/1",
                score=0.99,
            )
        ]


def register(contrib: Any) -> None:
    contrib.add_vector_index(DemoVectorIndex())
