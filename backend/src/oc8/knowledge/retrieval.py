# backend/src/oc8/knowledge/retrieval.py
"""Access-scoped KB retrieval (§11.4-11.5, minimal real loop): cosine-only
vector search over KnowledgeGrant-scoped chunks, filtered by classification
clearance (§5.3/§11.5/§12.4 — see docs/superpowers/specs/
2026-07-16-classification-enforcement-design.md). No hybrid/BM25, no
reranking."""

from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz import Effect, classification_rule, effective_cleared_classes
from oc8.knowledge.chunks import live_chunks
from oc8.modelrouter import EmbeddingUnavailable, get_model_router

KB_TOKEN_BUDGET = 1200
_CANDIDATE_LIMIT = 50
SIMILAR_CHUNKS_LIMIT = 5


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


def _trim_to_budget(
    chunks: list[m.KbChunk], query_embedding: list[float] | None, token_budget: int
) -> list[m.KbChunk]:
    def score(chunk: m.KbChunk) -> float:
        if query_embedding is None or chunk.embedding is None:
            return 0.0
        return _cosine_similarity(chunk.embedding, query_embedding)

    scored = sorted(chunks, key=score, reverse=True)
    out: list[m.KbChunk] = []
    used = 0
    for c in scored:
        cost = _estimate_tokens(c.content)
        if used + cost > token_budget:
            continue
        out.append(c)
        used += cost
    return out


async def retrieve_kb_context(
    db: AsyncSession,
    *,
    agent: m.Agent,
    tenant_id: uuid.UUID,
    query_text: str,
    frame: dict[str, Any] | None = None,
    model_locality: str = "cloud",
    token_budget: int = KB_TOKEN_BUDGET,
    only_kb_ids: frozenset[uuid.UUID] | None = None,
) -> tuple[str, bool]:
    """Returns (context, contains_restricted). frame/model_locality default
    to the safe/conservative values (no cleared_classes override, cloud
    locality) so existing callers that don't pass them get the strictest
    behavior — run_agent (the only production caller) always passes both
    explicitly.

    `only_kb_ids` is what the operator attached to THIS turn with `#`
    (`chat/service.py`). It is INTERSECTED with the agent's grants, never
    substituted for them: a reference to a knowledge base this agent may not
    read narrows the search to nothing and returns "", rather than reading it
    or quietly falling back to everything. Same monotonic "can only tighten"
    rule the frame algebra uses -- None means no narrowing at all, which is
    every pre-existing caller."""
    kb_ids = await granted_kb_ids(db, agent=agent)
    if only_kb_ids is not None:
        # Intersection, never replacement. A `#` reference cannot reach a base
        # the agent has no grant to.
        kb_ids = {kb_id for kb_id in kb_ids if kb_id in only_kb_ids}
    if not kb_ids:
        return "", False

    try:
        query_embedding = await get_model_router().embed(query_text)
    except EmbeddingUnavailable:
        return "", False

    # `live_chunks()` and not `select(m.KbChunk)`: the `deleted_at IS NULL`
    # predicate has to be in the WHERE rather than in the Python filters below.
    # A tombstoned chunk keeps its embedding (that is what makes restore cheap),
    # so it competes for the fifty candidate slots this LIMIT materialises before
    # any Python runs -- enough deleted chunks and the base goes silent. And a
    # reduced chunk has no content but keeps its `source_uri`, which line 124
    # renders verbatim: `upload://<id>/john-doe-contract.pdf` in the preamble is
    # a disclosure, not a cosmetic bug.
    stmt = (
        live_chunks()
        .where(m.KbChunk.tenant_id == tenant_id, m.KbChunk.kb_id.in_(kb_ids))
        .order_by(m.KbChunk.embedding.cosine_distance(query_embedding))
        .limit(_CANDIDATE_LIMIT)
    )
    candidates = list((await db.execute(stmt)).scalars().all())
    if not candidates:
        return "", False

    cleared = effective_cleared_classes(frame or {})
    cleared_candidates = [
        c
        for c in candidates
        if classification_rule(c.classification, cleared, model_locality).effect is Effect.ALLOW
    ]
    if not cleared_candidates:
        return "", False

    selected = _trim_to_budget(cleared_candidates, query_embedding, token_budget)
    if not selected:
        return "", False

    contains_restricted = any(c.classification == "restricted" for c in selected)

    by_kb: dict[uuid.UUID, list[m.KbChunk]] = {}
    for c in selected:
        by_kb.setdefault(c.kb_id, []).append(c)

    sections: list[str] = []
    for kb_id, kb_chunks in by_kb.items():
        kb = await db.get(m.KnowledgeBase, kb_id)
        kb_name = kb.name if kb is not None else str(kb_id)
        body = "\n".join(f"- {c.content} (source: {c.source_uri})" for c in kb_chunks)
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
