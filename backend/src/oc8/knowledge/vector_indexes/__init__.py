"""Query-only vector-index backends for external knowledge bases."""

from oc8.knowledge.vector_indexes.base import VectorHit, VectorIndex, VectorIndexError
from oc8.knowledge.vector_indexes.registry import (
    available_vector_indexes,
    resolve_vector_index,
)

__all__ = [
    "VectorHit",
    "VectorIndex",
    "VectorIndexError",
    "available_vector_indexes",
    "resolve_vector_index",
]
