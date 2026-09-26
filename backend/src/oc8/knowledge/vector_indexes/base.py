"""The vector-index seam: query an existing remote collection without ingest.

Connectors *ingest* into ``kb_chunk``. A ``VectorIndex`` only *searches* a
store the operator already filled elsewhere. Core never names a vendor; capas
contribute implementations via ``PluginContributions.add_vector_index``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class VectorIndexError(Exception):
    """Unknown type, invalid config, missing credential, or a failed probe."""


@dataclass(frozen=True)
class VectorHit:
    """One remote search result, shaped for retrieval merge + classification."""

    content: str
    source_uri: str
    score: float = 0.0
    classification: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class VectorIndex(Protocol):
    """A query-only retrieval backend for an already-populated collection."""

    type_id: str
    label: str | None
    description: str | None
    #: JSON Schema for non-secret ``index_config`` (collection, field mapping).
    config_schema: dict[str, object]
    #: Credential-type name declared by the capa (e.g. ``qdrant_api``).
    credential_type: str

    async def validate(
        self,
        config: dict[str, Any],
        auth: Any,
        *,
        credential_id: str,
    ) -> None:
        """Ping the store and confirm the collection/table exists.

        Raises ``VectorIndexError`` on failure. ``auth`` is a
        ``SourceAuthContext`` (or anything with ``credential(id, field)``).
        """
        ...

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
        """Nearest neighbours for ``query_embedding``. Never writes."""
        ...
