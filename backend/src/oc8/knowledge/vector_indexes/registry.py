"""Tenant-scoped vector-index lookup.

Mirrors ``oc8.knowledge.connectors.registry``: contribution is process-wide
(keyed by plugin id); resolution is always filtered by the tenant's installed
*and enabled* plugin set. There is no built-in remote index -- ``internal``
is the local ``kb_chunk`` path and never goes through this registry.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.capas.contributions import vector_indexes_for
from oc8.knowledge.connectors.registry import knowledge_plugin_gate, load_contributing_plugin
from oc8.knowledge.vector_indexes.base import VectorIndex, VectorIndexError

#: Stored on ``KnowledgeBase.index_type`` for the built-in ``kb_chunk`` path.
INTERNAL_INDEX_TYPE = "internal"


async def _plugin_vector_indexes(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, VectorIndex]:
    """Indexes from the plugins whose knowledge contributions this tenant may
    use -- the same gate as connectors, deferred ``knowledge:write`` included."""
    out: dict[str, VectorIndex] = {}
    usable, _pending = await knowledge_plugin_gate(db, tenant_id)
    for name in usable:
        if load_contributing_plugin(name):
            out.update(vector_indexes_for(name))
    return out


async def resolve_vector_index(
    db: AsyncSession, *, tenant_id: uuid.UUID, type_id: str
) -> VectorIndex:
    """The tenant-scoped lookup. Fail closed on unknown / not enabled."""
    if type_id == INTERNAL_INDEX_TYPE:
        raise VectorIndexError(
            f"{INTERNAL_INDEX_TYPE!r} is the built-in path, not a vector-index capa"
        )
    contributed = await _plugin_vector_indexes(db, tenant_id)
    index = contributed.get(type_id)
    if index is None:
        raise VectorIndexError(f"unknown vector index type: {type_id!r}")
    return index


async def available_vector_indexes(
    db: AsyncSession, *, tenant_id: uuid.UUID
) -> dict[str, VectorIndex]:
    """Index implementations this tenant may use (enabled capas only)."""
    return await _plugin_vector_indexes(db, tenant_id)
