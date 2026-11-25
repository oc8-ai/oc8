# backend/src/oc8/knowledge/retrieval.py
"""Access-scoped KB retrieval (§11.4-11.5): hybrid search over granted KBs.

Internal bases query ``kb_chunk`` twice -- by cosine distance and by full text
(``websearch_to_tsquery('simple', q)``, which finds an invoice number or an
article code verbatim where cosine finds only what sounds alike). External
bases (``index_type`` ≠ ``internal``) dispatch to a capa ``VectorIndex``. Every
ranked list is fused with reciprocal rank fusion; all paths share grants,
classification clearance, and the token budget.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz import Effect, classification_rule, effective_cleared_classes
from oc8.knowledge.chunks import live_chunks
from oc8.knowledge.connectors.context import SourceAuthContext
from oc8.knowledge.embedding_policy import (
    LOCAL_AGENT_REASON,
    agent_runs_locally,
    require_local_reason,
    strictest,
)
from oc8.knowledge.vector_indexes.base import VectorIndexError
from oc8.knowledge.vector_indexes.registry import INTERNAL_INDEX_TYPE, resolve_vector_index
from oc8.modelrouter import EmbeddingMustStayLocal, EmbeddingUnavailable, get_model_router
from oc8.models.knowledge import KB_KIND_SHARED

logger = logging.getLogger(__name__)

KB_TOKEN_BUDGET = 1200
_CANDIDATE_LIMIT = 50
SIMILAR_CHUNKS_LIMIT = 5
#: Reciprocal rank fusion constant: a hit at rank r in a list adds 1 / (k + r).
RRF_K = 60
#: Rows an HNSW scan of `kb_chunk.embedding` (migration 0137) hands back before
#: the tenant/base/source filter. At pgvector's default of 40 a base whose
#: nearest rows sit among other bases' rows got fewer than `_CANDIDATE_LIMIT`
#: candidates, or none, where the exact scan before the index found them all.
HNSW_EF_SEARCH = 4 * _CANDIDATE_LIMIT
#: pgvector's version as `(major, minor)`, read once per process: `None` until
#: read, `()` when the extension is absent.
_pgvector_version: tuple[int, ...] | None = None
#: A question full-text search can use at all; without a word character
#: `websearch_to_tsquery` yields an empty query (and a NOTICE per base).
_HAS_WORD = re.compile(r"\w")


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
    #: The `kb_chunk` row of an internal hit; None for a remote one.
    chunk_id: uuid.UUID | None = None
    #: Set by `fuse_rrf`; what the budget trim ranks by. `score` stays cosine.
    rank_score: float = 0.0


def _identity(hit: _Hit) -> tuple[Any, ...]:
    if hit.chunk_id is not None:
        return ("chunk", hit.chunk_id)
    digest = hashlib.sha1(hit.content.encode(), usedforsecurity=False).hexdigest()
    return ("remote", hit.kb_id, hit.source_uri, digest)


def fuse_rrf(ranked_lists: list[list[_Hit]], *, k: int = RRF_K) -> list[_Hit]:
    """Reciprocal rank fusion: each hit scores the sum of 1 / (k + rank) over
    the lists it appears in (rank from 1), and comes back once, highest first,
    with `rank_score` set. A hit is its chunk for an internal base, and its
    base, source and text for a remote one. Ties keep first-seen order, so
    callers pass each base's vector list before its full-text list and a tie
    goes to the better vector rank."""
    fused: dict[tuple[Any, ...], _Hit] = {}
    for ranked in ranked_lists:
        for rank, hit in enumerate(ranked, start=1):
            key = _identity(hit)
            kept = fused.setdefault(key, hit)
            if kept is hit:
                hit.rank_score = 0.0
            kept.rank_score += 1.0 / (k + rank)
    return sorted(fused.values(), key=lambda h: h.rank_score, reverse=True)


async def granted_kb_ids(db: AsyncSession, *, agent: m.Agent) -> set[uuid.UUID]:
    result = await db.execute(
        select(m.KnowledgeGrant.kb_id).where(
            m.KnowledgeGrant.grantee_id.in_([agent.id, agent.department_id])
        )
    )
    return set(result.scalars().all())


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _trim_hits_to_budget(hits: list[_Hit], token_budget: int) -> list[_Hit]:
    scored = sorted(hits, key=lambda h: h.rank_score, reverse=True)
    out: list[_Hit] = []
    used = 0
    for h in scored:
        cost = _estimate_tokens(h.content)
        if used + cost > token_budget:
            continue
        out.append(h)
        used += cost
    return out


async def _embed_for_kb(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb: m.KnowledgeBase,
    query_text: str,
    local_agent: bool,
    skipped_local: list[str] | None = None,
) -> list[float] | None:
    """Query embedding with the KB's own model, through the tenant's own
    provider credentials. Skip this KB on failure -- at WARNING with the
    reason, because a base that silently returns nothing is otherwise
    indistinguishable from one that has no relevant content.

    The query stays local when the base must (restricted, local-only) and
    when the agent runs on a local model: such an agent may hold restricted
    material, and the question it searches with is derived from it -- the
    same rule memory embeddings follow."""
    require_local = require_local_reason(kb=kb) or (LOCAL_AGENT_REASON if local_agent else None)
    try:
        return await get_model_router().embed(
            query_text,
            model=kb.embedding_model,
            tenant_id=tenant_id,
            db=db,
            require_local=require_local,
        )
    except EmbeddingUnavailable as exc:
        # A base whose cloud embedding the question may not reach: the caller
        # tells the member, who otherwise sees the base as merely empty.
        if isinstance(exc, EmbeddingMustStayLocal) and skipped_local is not None:
            skipped_local.append(kb.name)
        logger.warning(
            "embedding unavailable for kb %s model %s, skipping: %s",
            kb.id,
            kb.embedding_model,
            exc,
        )
        return None
    except Exception:
        logger.exception("embed failed for kb %s — skipping", kb.id)
        return None


async def _widen_hnsw_scan(db: AsyncSession) -> None:
    """Let an HNSW scan return enough rows for the filtered candidate list,
    for the rest of this transaction: `hnsw.ef_search`, and
    `hnsw.iterative_scan = relaxed_order`, which keeps scanning until the
    filtered LIMIT is met (the order may be slightly off; fusion re-sorts
    anyway). Lasting to the end of the transaction is harmless: it only
    widens a later HNSW scan of `kb_chunk` in the same transaction the same
    way, and no other table has an HNSW index.

    Only from pgvector 0.8.0, the version migration 0137 builds the index
    from: below it there is no index to widen. And pgvector reserves the
    `hnsw.` prefix, so setting a name an older version does not know is an
    error that would abort the transaction."""
    global _pgvector_version
    if _pgvector_version is None:
        extversion = (
            await db.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
        ).scalar_one_or_none()
        _pgvector_version = (
            tuple(int(p) for p in extversion.split(".")[:2] if p.isdigit()) if extversion else ()
        )
    if _pgvector_version < (0, 8):
        return
    await db.execute(
        text(
            "SELECT set_config('hnsw.ef_search', :ef, true), "
            "set_config('hnsw.iterative_scan', 'relaxed_order', true)"
        ),
        {"ef": str(HNSW_EF_SEARCH)},
    )


async def _search_internal(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb: m.KnowledgeBase,
    query_embedding: list[float],
    query_text: str,
    only_sources: Mapping[str, str] | None = None,
) -> tuple[list[_Hit], list[_Hit]]:
    """(top by cosine, top by full text), each at most `_CANDIDATE_LIMIT` and
    under the same filters. The full-text list is empty for a question with
    no words in it (`#`, `--`, blank)."""
    stmt = live_chunks().where(m.KbChunk.tenant_id == tenant_id, m.KbChunk.kb_id == kb.id)
    if only_sources is not None:
        # In the query, so other documents cannot crowd these out of the
        # candidate limit.
        stmt = stmt.where(m.KbChunk.source_uri.in_(sorted(only_sources)))
    await _widen_hnsw_scan(db)
    by_vector = stmt.order_by(m.KbChunk.embedding.cosine_distance(query_embedding)).limit(
        _CANDIDATE_LIMIT
    )
    vector_chunks = list((await db.execute(by_vector)).scalars().all())
    text_chunks: list[m.KbChunk] = []
    if _HAS_WORD.search(query_text):
        tsq = func.websearch_to_tsquery("simple", query_text)
        # `numnode` is 0 for a query without lexemes; the planner folds the
        # constant, so such a query reads no rows at all.
        by_text = (
            stmt.where(func.numnode(tsq) > 0, m.KbChunk.search_tsv.bool_op("@@")(tsq))
            .order_by(func.ts_rank_cd(m.KbChunk.search_tsv, tsq).desc(), m.KbChunk.id)
            .limit(_CANDIDATE_LIMIT)
        )
        text_chunks = list((await db.execute(by_text)).scalars().all())
    # A chunk's own label may be looser than its source's or its base's: chunks
    # stored before ingest copied those labels read `internal`, and a label
    # changed later on the base or source is not rewritten into every chunk.
    # Judge each hit by the strictest of the three.
    source_ids = {
        c.data_source_id for c in (*vector_chunks, *text_chunks) if c.data_source_id is not None
    }
    source_labels: dict[uuid.UUID, str] = {}
    if source_ids:
        source_labels = {
            row.id: row.classification
            for row in (
                await db.execute(
                    select(m.DataSource.id, m.DataSource.classification).where(
                        m.DataSource.tenant_id == tenant_id, m.DataSource.id.in_(source_ids)
                    )
                )
            ).all()
        }

    def to_hit(c: m.KbChunk) -> _Hit:
        score = 0.0
        if c.embedding is not None:
            score = cosine_similarity(c.embedding, query_embedding)
        return _Hit(
            kb_id=kb.id,
            content=c.content,
            source_uri=c.source_uri,
            classification=strictest(
                c.classification,
                kb.classification,
                source_labels.get(c.data_source_id) if c.data_source_id else None,
            ),
            score=score,
            local_only=kb.local_only,
            chunk_id=c.id,
        )

    return [to_hit(c) for c in vector_chunks], [to_hit(c) for c in text_chunks]


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
        # The index may label its own hits, but never more loosely than the base.
        classification = strictest(r.classification, kb.classification)
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
    only_kb_ids: frozenset[uuid.UUID] | None = None,
    released_kb_ids: frozenset[uuid.UUID] | None = None,
    skipped_local: list[str] | None = None,
    only_sources: Mapping[str, str] | None = None,
) -> tuple[str, bool]:
    """Returns (context, contains_restricted). `contains_restricted` is true
    when any returned text is restricted or from a local-only base: text that
    may only reach a local model. frame/model_locality default
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
    every pre-existing caller.

    `released_kb_ids` are the bases the member pinned to the chat or its
    project (oc8 Chat §5.5, `chat.context.manifest`): THEIR release, made under
    their own `knowledge:view`, so these are searched whether or not the agent
    holds a grant, IN ADDITION to the agent's own grants (narrowed by
    `only_kb_ids` when given). The caller re-checks the member's right to
    each one first. Classification and the model's locality still filter
    every hit below.

    `skipped_local`, when given, receives the name of every base that was not
    searched because the question must stay local and the base embeds with a
    cloud model.

    A chat's own base (`kind='chat'`, spec §5.4) is searched ONLY when the
    caller names it in `only_kb_ids` (`search_chat_files`). Not through a
    grant, not through `released_kb_ids` alone: nothing that feeds those sets
    for an agent's normal search may ever reach a chat's files, so even a
    stray grant or pin on one is ignored here.

    `only_sources`, when given, maps the `source_uri`s that may be returned
    to the name each is shown under: every other document is left out, and a
    hit's source is shown by that name (`search_chat_files`: the files on
    the branch a run answers, named by the file the model can read)."""
    kb_ids = await granted_kb_ids(db, agent=agent)
    if only_kb_ids is not None:
        # Intersection, never replacement. A `#` reference cannot reach a base
        # the agent has no grant to.
        kb_ids = {kb_id for kb_id in kb_ids if kb_id in only_kb_ids}
    if released_kb_ids:
        # In addition to the grants (Ruling 34): pinning never makes the agent
        # know less. Only a `#` on the message narrows, and only the grants.
        kb_ids = set(kb_ids) | set(released_kb_ids)
    if not kb_ids:
        return "", False

    bases = list(
        (
            await db.execute(
                select(m.KnowledgeBase).where(
                    m.KnowledgeBase.tenant_id == tenant_id,
                    m.KnowledgeBase.id.in_(kb_ids),
                    m.KnowledgeBase.deleted_at.is_(None),
                    or_(
                        m.KnowledgeBase.kind == KB_KIND_SHARED,
                        m.KnowledgeBase.id.in_(sorted(only_kb_ids or ())),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if not bases:
        return "", False

    local_agent = await agent_runs_locally(db, agent, model_locality)
    # Each internal base's vector list, then its full-text list; each external
    # base's one remote list. Fused once, across bases, before any filter.
    ranked_lists: list[list[_Hit]] = []
    for kb in bases:
        query_embedding = await _embed_for_kb(
            db,
            tenant_id=tenant_id,
            kb=kb,
            query_text=query_text,
            local_agent=local_agent,
            skipped_local=skipped_local,
        )
        if query_embedding is None:
            continue
        index_type = kb.index_type or INTERNAL_INDEX_TYPE
        if index_type == INTERNAL_INDEX_TYPE:
            ranked_lists.extend(
                await _search_internal(
                    db,
                    tenant_id=tenant_id,
                    kb=kb,
                    query_embedding=query_embedding,
                    query_text=query_text,
                    only_sources=only_sources,
                )
            )
        else:
            ranked_lists.append(
                await _search_external(
                    db,
                    tenant_id=tenant_id,
                    kb=kb,
                    query_embedding=query_embedding,
                    query_text=query_text,
                )
            )

    all_hits = fuse_rrf(ranked_lists)
    if only_sources is not None:
        all_hits = [h for h in all_hits if h.source_uri in only_sources]
    if not all_hits:
        return "", False

    cleared = effective_cleared_classes(frame or {})
    # A local-only base's text reaches a local model only, like restricted text.
    cleared_hits = [
        h
        for h in all_hits
        if classification_rule(h.classification, cleared, model_locality).effect is Effect.ALLOW
        and (not h.local_only or model_locality == "local")
    ]
    if not cleared_hits:
        return "", False

    selected = _trim_hits_to_budget(cleared_hits, token_budget)
    if not selected:
        return "", False

    # Marks the run: the router then refuses a cloud model for it (and for any
    # fallback), and the department cache skips it.
    contains_restricted = any(h.classification == "restricted" or h.local_only for h in selected)

    by_kb: dict[uuid.UUID, list[_Hit]] = {}
    for h in selected:
        by_kb.setdefault(h.kb_id, []).append(h)

    sections: list[str] = []
    for kb_id, kb_hits in by_kb.items():
        kb = next((b for b in bases if b.id == kb_id), None)
        kb_name = kb.name if kb is not None else str(kb_id)
        body = "\n".join(
            f"- {h.content} (source: {(only_sources or {}).get(h.source_uri, h.source_uri)})"
            for h in kb_hits
        )
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
    # The same HNSW post-filter cap as `_search_internal`: other bases' rows
    # would otherwise crowd this base's neighbours out of the scan.
    await _widen_hnsw_scan(db)
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
                similarity=cosine_similarity(source.embedding, c.embedding),
            )
        )
    return result
