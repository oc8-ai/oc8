# backend/src/oc8/knowledge/retrieval.py
"""Access-scoped KB retrieval (§11.4-11.5): cosine search over granted KBs.

Internal bases query ``kb_chunk``. External bases (``index_type`` ≠
``internal``) dispatch to a capa ``VectorIndex``. Both paths share grants,
classification clearance, and the token budget.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz import Effect, classification_rule, effective_cleared_classes
from oc8.knowledge.chunks import live_chunks
from oc8.knowledge.connectors.context import SourceAuthContext
from oc8.knowledge.vector_indexes.base import VectorIndexError
from oc8.knowledge.vector_indexes.registry import INTERNAL_INDEX_TYPE, resolve_vector_index
from oc8.modelrouter import EmbeddingUnavailable, get_model_router

logger = logging.getLogger(__name__)

KB_TOKEN_BUDGET = 1200
_CANDIDATE_LIMIT = 50
SIMILAR_CHUNKS_LIMIT = 5


@dataclass
class _Hit:
    """Unified candidate for budget trim + render (internal or remote)."""

    kb_id: uuid.UUID
    content: str
    source_uri: str
    classification: str
    score: float
    local_only: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


async def granted_kb_ids(db: AsyncSession, *, agent: m.Agent) -> set[uuid.UUID]:
    result = await db.execute(
        select(m.KnowledgeGrant.kb_id).where(
            m.KnowledgeGrant.grantee_id.in_([agent.id, agent.department_id])
        )
    )
    return set(result.scalars().all())


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _trim_hits_to_budget(hits: list[_Hit], token_budget: int) -> list[_Hit]:
    scored = sorted(hits, key=lambda h: h.score, reverse=True)
    out: list[_Hit] = []
    used = 0
    for h in scored:
        cost = _estimate_tokens(h.content)
        if used + cost > token_budget:
            continue
        out.append(h)
        used += cost
    return out


async def _embed_for_kb(kb: m.KnowledgeBase, query_text: str) -> list[float] | None:
    """Query embedding with the KB's own model. Skip this KB on failure."""
    try:
        return await get_model_router().embed(query_text, model=kb.embedding_model)
    except EmbeddingUnavailable:
        logger.info(
            "embedding unavailable for kb %s model %s — skipping",
            kb.id,
            kb.embedding_model,
        )
        return None
    except Exception:
        logger.exception("embed failed for kb %s — skipping", kb.id)
        return None


async def _search_internal(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb: m.KnowledgeBase,
    query_embedding: list[float],
) -> list[_Hit]:
    stmt = (
        live_chunks()
        .where(m.KbChunk.tenant_id == tenant_id, m.KbChunk.kb_id == kb.id)
        .order_by(m.KbChunk.embedding.cosine_distance(query_embedding))
        .limit(_CANDIDATE_LIMIT)
    )
    chunks = list((await db.execute(stmt)).scalars().all())
    hits: list[_Hit] = []
    for c in chunks:
        score = 0.0
        if c.embedding is not None:
            score = _cosine_similarity(c.embedding, query_embedding)
        hits.append(
            _Hit(
                kb_id=kb.id,
                content=c.content,
                source_uri=c.source_uri,
                classification=c.classification,
                score=score,
                local_only=kb.local_only,
            )
        )
    return hits


async def _search_external(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb: m.KnowledgeBase,
    query_embedding: list[float],
    query_text: str,
) -> list[_Hit]:
    if kb.credential_id is None:
        logger.info("external kb %s has no credential_id — skipping", kb.id)
        return []
    try:
        index = await resolve_vector_index(db, tenant_id=tenant_id, type_id=kb.index_type)
        auth = SourceAuthContext(db, tenant_id=tenant_id)
        remote = await index.search(
            kb.index_config or {},
            auth,
            credential_id=str(kb.credential_id),
            query_embedding=query_embedding,
            query_text=query_text,
            limit=_CANDIDATE_LIMIT,
        )
    except VectorIndexError:
        logger.info("vector index search failed for kb %s — skipping", kb.id, exc_info=True)
        return []
    except Exception:
        logger.exception("vector index search errored for kb %s — skipping", kb.id)
        return []

    hits: list[_Hit] = []
    for r in remote:
        classification = r.classification or kb.classification
        hits.append(
            _Hit(
                kb_id=kb.id,
                content=r.content,
                source_uri=r.source_uri or f"index://{kb.id}",
                classification=classification,
                score=r.score,
                local_only=kb.local_only,
                metadata=dict(r.metadata),
            )
        )
    return hits


async def retrieve_kb_context(
    db: AsyncSession,
    *,
    agent: m.Agent,
    tenant_id: uuid.UUID,
    query_text: str,
    frame: dict[str, Any] | None = None,
    model_locality: str = "cloud",
    token_budget: int = KB_TOKEN_BUDGET,
) -> tuple[str, bool]:
    """Returns (context, contains_restricted). frame/model_locality default
    to the safe/conservative values (no cleared_classes override, cloud
    locality) so existing callers that don't pass them get the strictest
    behavior — run_agent (the only production caller) always passes both
    explicitly."""
    kb_ids = await granted_kb_ids(db, agent=agent)
    if not kb_ids:
        return "", False

    bases = list(
        (
            await db.execute(
                select(m.KnowledgeBase).where(
                    m.KnowledgeBase.tenant_id == tenant_id,
                    m.KnowledgeBase.id.in_(kb_ids),
                    m.KnowledgeBase.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not bases:
        return "", False

    all_hits: list[_Hit] = []
    for kb in bases:
        query_embedding = await _embed_for_kb(kb, query_text)
        if query_embedding is None:
            continue
        index_type = kb.index_type or INTERNAL_INDEX_TYPE
        if index_type == INTERNAL_INDEX_TYPE:
            all_hits.extend(
                await _search_internal(
                    db, tenant_id=tenant_id, kb=kb, query_embedding=query_embedding
                )
            )
        else:
            all_hits.extend(
                await _search_external(
                    db,
                    tenant_id=tenant_id,
                    kb=kb,
                    query_embedding=query_embedding,
                    query_text=query_text,
                )
            )

    if not all_hits:
        return "", False

    cleared = effective_cleared_classes(frame or {})
    cleared_hits = [
        h
        for h in all_hits
        if classification_rule(h.classification, cleared, model_locality).effect is Effect.ALLOW
    ]
    if not cleared_hits:
        return "", False

    selected = _trim_hits_to_budget(cleared_hits, token_budget)
    if not selected:
        return "", False

    contains_restricted = any(h.classification == "restricted" for h in selected)

    by_kb: dict[uuid.UUID, list[_Hit]] = {}
    for h in selected:
        by_kb.setdefault(h.kb_id, []).append(h)

    sections: list[str] = []
    for kb_id, kb_hits in by_kb.items():
        kb = next((b for b in bases if b.id == kb_id), None)
        kb_name = kb.name if kb is not None else str(kb_id)
        body = "\n".join(f"- {h.content} (source: {h.source_uri})" for h in kb_hits)
        sections.append(f"[Knowledge: {kb_name}]\n{body}")

    return "\n\n".join(sections), contains_restricted


@dataclass(frozen=True)
class SimilarChunkRow:
    """A plain, non-ORM view of one chunk nearest to a query chunk -- the
    read-path guard forbids `KbChunk` itself from leaving this module (or the
    other allowlisted modules), so this is what the API layer and
    serializers work with instead."""

    id: uuid.UUID
    kb_id: uuid.UUID
    source_uri: str
    content: str
    classification: str
    chunk_metadata: dict[str, Any]
    created_at: dt.datetime
    similarity: float


async def similar_chunks(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    chunk_id: uuid.UUID,
    limit: int = SIMILAR_CHUNKS_LIMIT,
) -> list[SimilarChunkRow] | None:
    """Top-`limit` live chunks in the same KB nearest to `chunk_id` by cosine
    similarity, excluding `chunk_id` itself. Returns `None` if `chunk_id`
    doesn't exist, isn't live, isn't in `kb_id`, or has no embedding yet --
    the caller turns that into a 404."""
    source = (
        await db.execute(
            live_chunks().where(
                m.KbChunk.tenant_id == tenant_id,
                m.KbChunk.kb_id == kb_id,
                m.KbChunk.id == chunk_id,
            )
        )
    ).scalar_one_or_none()
    if source is None or source.embedding is None:
        return None
    stmt = (
        live_chunks()
        .where(
            m.KbChunk.tenant_id == tenant_id,
            m.KbChunk.kb_id == kb_id,
            m.KbChunk.id != chunk_id,
            m.KbChunk.embedding.is_not(None),
        )
        .order_by(m.KbChunk.embedding.cosine_distance(source.embedding))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    result = []
    for c in rows:
        assert c.embedding is not None  # narrowed by the `is_not(None)` predicate above
        result.append(
            SimilarChunkRow(
                id=c.id,
                kb_id=c.kb_id,
                source_uri=c.source_uri,
                content=c.content,
                classification=c.classification,
                chunk_metadata=c.chunk_metadata,
                created_at=c.created_at,
                similarity=_cosine_similarity(source.embedding, c.embedding),
            )
        )
    return result
