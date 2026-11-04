"""A tombstoned chunk must not reach the agent -- not its text, not its URI.

The predicate has to be in the SQL WHERE rather than in the Python post-filters,
and both reasons here are real rather than stylistic. A tombstoned-but-unreduced
chunk still carries its embedding (that is what makes restore cheap), so it
competes for the fifty candidate slots the query materialises BEFORE any Python
runs. And a reduced chunk has no content but keeps its `source_uri`, which is
exactly the field that can be personal data.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.knowledge.retrieval import retrieve_kb_context
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

QUERY = "what does the handbook say about overtime"


def _vec(text: str) -> list[float]:
    seed = sum(ord(c) for c in text) or 1
    return [float((seed + i) % 23) for i in range(EMBED_DIM)]


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        return _vec(text)


async def _kb(db: Any, tenant: uuid.UUID) -> m.KnowledgeBase:
    kb = m.KnowledgeBase(tenant_id=tenant, name="Handbook", embedding_model="nomic-embed-text")
    db.add(kb)
    await db.flush()
    return kb


async def _granted_agent(db: Any, tenant: uuid.UUID, kb_id: uuid.UUID) -> m.Agent:
    agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
    db.add(agent)
    await db.flush()
    db.add(
        m.KnowledgeGrant(tenant_id=tenant, kb_id=kb_id, grantee_type="agent", grantee_id=agent.id)
    )
    await db.flush()
    return agent


async def test_tombstoned_chunks_do_not_consume_candidate_slots(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sixty dead chunks sitting exactly on the query, one live chunk further
    away. `ORDER BY embedding <=> :q LIMIT 50` materialises its fifty candidates
    in the database, so a Python filter would hand back an empty list and the
    knowledge base would go silent the moment enough of it was deleted.
    """
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    on_the_query = _vec(QUERY)
    further_away = list(reversed(on_the_query))
    now = dt.datetime.now(tz=dt.UTC)

    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        agent = await _granted_agent(db, tenant, kb.id)
        for i in range(60):
            db.add(
                m.KbChunk(
                    tenant_id=tenant,
                    kb_id=kb.id,
                    content=f"deleted paragraph {i}",
                    embedding=on_the_query,
                    source_uri=f"upload://gone/{i}.txt",
                    chunk_metadata={"chunk_index": 0},
                    deleted_at=now,
                    deleted_reason="source_absent",
                )
            )
        db.add(
            m.KbChunk(
                tenant_id=tenant,
                kb_id=kb.id,
                content="overtime is compensated in time off",
                embedding=further_away,
                source_uri="upload://live/handbook.txt",
                chunk_metadata={"chunk_index": 0},
            )
        )
        await db.flush()

        ctx, _ = await retrieve_kb_context(db, agent=agent, tenant_id=tenant, query_text=QUERY)

        assert "overtime is compensated in time off" in ctx
        assert "deleted paragraph" not in ctx


async def test_a_reduced_chunk_never_leaks_its_source_uri_into_the_preamble(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reduced chunk has no content left, but retrieval renders
    `- {content} (source: {source_uri})` -- so a base with fewer than fifty live
    chunks would copy the URI of every erased document straight into the agent's
    preamble, at score 0.0 and one token each. `upload://<id>/john-doe-contract.pdf`
    is the shape that makes this a disclosure rather than a cosmetic bug.
    """
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    tenant = uuid.uuid4()
    now = dt.datetime.now(tz=dt.UTC)
    erased = [f"upload://{uuid.uuid4()}/john-doe-contract-{i}.pdf" for i in range(5)]

    async with app_session(tenant) as db:
        kb = await _kb(db, tenant)
        agent = await _granted_agent(db, tenant, kb.id)
        for i in range(3):
            db.add(
                m.KbChunk(
                    tenant_id=tenant,
                    kb_id=kb.id,
                    content=f"live paragraph {i} about overtime",
                    embedding=_vec(f"live {i}"),
                    source_uri=f"upload://live/handbook-{i}.txt",
                    chunk_metadata={"chunk_index": 0},
                )
            )
        for uri in erased:
            db.add(
                m.KbChunk(
                    tenant_id=tenant,
                    kb_id=kb.id,
                    content="",
                    embedding=None,
                    source_uri=uri,
                    chunk_metadata={"chunk_index": 0},
                    deleted_at=now,
                    deleted_reason="operator_delete",
                    reduced_at=now,
                )
            )
        await db.flush()

        ctx, _ = await retrieve_kb_context(db, agent=agent, tenant_id=tenant, query_text=QUERY)

        assert "live paragraph" in ctx
        for uri in erased:
            assert uri not in ctx
        assert "john-doe-contract" not in ctx
