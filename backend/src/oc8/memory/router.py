# backend/src/oc8/memory/router.py
"""Memory Router: the single PEP for agent memory reads and writes across
the three §10 tiers (agent/department/company). Retrieval blends cosine
similarity with recency, budget-trimmed per tier; company-tier writes are
created immediately at status="pending" (invisible to retrieval) and flip
to "approved"/"rejected" via resolve_memory_write_approval."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.memory.policy import authorize_memory_read
from oc8.modelrouter import EmbeddingUnavailable, get_model_router

MAX_MEMORY_CONTENT_LENGTH = 4000
_CANDIDATE_LIMIT = 50
_W_SIM = 0.7
_W_RECENCY = 0.3
_HALF_LIFE_DAYS = 14.0
_TIER_LABELS = {"agent": "Agent", "department": "Department", "company": "Company"}


class MemoryWriteError(RuntimeError):
    pass


def _owner_id(tier: str, *, agent: m.Agent, tenant_id: uuid.UUID) -> uuid.UUID:
    return {"agent": agent.id, "department": agent.department_id, "company": tenant_id}[tier]


async def _tier_store(
    db: AsyncSession, *, tenant_id: uuid.UUID, tier: str, owner_id: uuid.UUID
) -> m.MemoryStore | None:
    return (
        await db.execute(
            select(m.MemoryStore).where(
                m.MemoryStore.tenant_id == tenant_id,
                m.MemoryStore.tier == tier,
                m.MemoryStore.owner_id == owner_id,
            )
        )
    ).scalar_one_or_none()


async def _get_or_create_store(
    db: AsyncSession, *, tenant_id: uuid.UUID, tier: str, owner_id: uuid.UUID
) -> m.MemoryStore:
    store = await _tier_store(db, tenant_id=tenant_id, tier=tier, owner_id=owner_id)
    if store is None:
        store = m.MemoryStore(tenant_id=tenant_id, tier=tier, owner_id=owner_id)
        db.add(store)
        await db.flush()
    return store


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _recency_decay(created_at: datetime) -> float:
    age_days = (datetime.now(tz=UTC) - created_at).total_seconds() / 86400
    return math.exp(-max(age_days, 0.0) / _HALF_LIFE_DAYS)


def _blend_score(record: m.MemoryRecord, query_embedding: list[float] | None) -> float:
    recency = _recency_decay(record.created_at)
    if query_embedding is None or record.embedding is None:
        return recency * _W_RECENCY
    sim = _cosine_similarity(record.embedding, query_embedding)
    return sim * _W_SIM + recency * _W_RECENCY


def _trim_to_budget(
    records: list[m.MemoryRecord], query_embedding: list[float] | None, token_budget: int
) -> list[m.MemoryRecord]:
    scored = sorted(records, key=lambda r: _blend_score(r, query_embedding), reverse=True)
    out: list[m.MemoryRecord] = []
    used = 0
    for r in scored:
        cost = _estimate_tokens(r.content)
        if used + cost > token_budget:
            continue
        out.append(r)
        used += cost
    return out


async def _candidates(
    db: AsyncSession,
    *,
    store_id: uuid.UUID,
    query_embedding: list[float] | None,
    member_id: uuid.UUID | None = None,
) -> list[m.MemoryRecord]:
    stmt = select(m.MemoryRecord).where(
        m.MemoryRecord.store_id == store_id, m.MemoryRecord.status == "approved"
    )
    if member_id is not None:
        stmt = stmt.where(m.MemoryRecord.record_metadata["member_id"].astext == str(member_id))
    if query_embedding is not None:
        stmt = stmt.order_by(m.MemoryRecord.embedding.cosine_distance(query_embedding))
    else:
        stmt = stmt.order_by(m.MemoryRecord.created_at.desc())
    stmt = stmt.limit(_CANDIDATE_LIMIT)
    return list((await db.execute(stmt)).scalars().all())


async def retrieve_context(
    db: AsyncSession,
    *,
    agent: m.Agent,
    tenant_id: uuid.UUID,
    frame: dict[str, Any],
    query_text: str,
    token_budget_per_tier: int = 800,
    narrowing: dict[str, Any] | None = None,
    member_id: uuid.UUID | None = None,
) -> str:
    """Recalled memory for `query_text`, from every tier the agent may read.

    `narrowing` is the run's PINNED narrowing (`resolve_version`), so which
    tiers a run may read is decided by the same version that decides which
    it may write (`authorize_memory_write`). Every runtime passes it; the
    live-row fallback is for a caller with no run behind it."""
    if narrowing is None:
        narrowing = agent.narrowing or {}
    try:
        query_embedding = await get_model_router().embed(query_text)
    except EmbeddingUnavailable:
        query_embedding = None

    sections: list[str] = []
    personal = agent.is_tenant_assistant
    for tier in ("agent", "department", "company"):
        if not authorize_memory_read(frame, narrowing, tier):
            continue
        if personal and tier == "department":
            # The Copilot's department is the synthetic Assistant department:
            # one store for every member, i.e. a cross-member channel (§7a.5).
            continue
        if personal and tier == "agent" and member_id is None:
            continue  # no member behind this turn: no personal notes at all
        owner_id = _owner_id(tier, agent=agent, tenant_id=tenant_id)
        store = await _tier_store(db, tenant_id=tenant_id, tier=tier, owner_id=owner_id)
        if store is None:
            continue
        candidates = await _candidates(
            db,
            store_id=store.id,
            query_embedding=query_embedding,
            member_id=member_id if (personal and tier == "agent") else None,
        )
        if not candidates:
            continue
        selected = _trim_to_budget(candidates, query_embedding, token_budget_per_tier)
        if not selected:
            continue
        body = "\n".join(f"- {r.content}" for r in selected)
        sections.append(f"[Memory — {_TIER_LABELS[tier]}]\n{body}")

    return "\n\n".join(sections)


async def write_memory(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent: m.Agent,
    tier: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    member_id: uuid.UUID | None = None,
) -> m.MemoryRecord:
    if tier not in ("agent", "department", "company"):
        raise MemoryWriteError(f"unknown tier '{tier}'")
    if not content or not content.strip():
        raise MemoryWriteError("content must not be empty")
    if len(content) > MAX_MEMORY_CONTENT_LENGTH:
        raise MemoryWriteError(f"content exceeds {MAX_MEMORY_CONTENT_LENGTH} characters")

    if agent.is_tenant_assistant:
        if tier != "agent":
            raise MemoryWriteError("the Copilot keeps personal notes only (tier 'agent')")
        if member_id is None:
            raise MemoryWriteError("no member behind this Copilot turn -- nothing to note for")
        metadata = {**(metadata or {}), "member_id": str(member_id)}

    owner_id = _owner_id(tier, agent=agent, tenant_id=tenant_id)
    store = await _get_or_create_store(db, tenant_id=tenant_id, tier=tier, owner_id=owner_id)

    try:
        embedding = await get_model_router().embed(content)
    except EmbeddingUnavailable:
        embedding = None

    record = m.MemoryRecord(
        tenant_id=tenant_id,
        store_id=store.id,
        content=content,
        embedding=embedding,
        record_metadata=metadata or {},
        written_by=agent.id,
        status="pending" if tier == "company" else "approved",
    )
    db.add(record)
    await db.flush()
    return record


async def resolve_memory_write_approval(
    db: AsyncSession, *, approval_request: m.ApprovalRequest, decision: str
) -> m.MemoryRecord:
    raw_id = (approval_request.payload or {}).get("memory_record_id")
    if not raw_id:
        raise MemoryWriteError("approval payload missing memory_record_id")
    record = await db.get(m.MemoryRecord, uuid.UUID(raw_id))
    if record is None:
        raise MemoryWriteError(f"memory record {raw_id} not found")
    record.status = "approved" if decision == "approve" else "rejected"
    await db.flush()
    return record
