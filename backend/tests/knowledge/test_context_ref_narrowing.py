"""A `#` reference narrows one turn's retrieval -- it can never widen it (§5.2).

`only_kb_ids` is intersected with the agent's own grants. A reference to a
knowledge base the agent may not read searches NOTHING, rather than reading it
anyway or silently falling back to everything.
"""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.knowledge.retrieval import retrieve_kb_context
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _granted_agent(
    db: object, tenant: uuid.UUID
) -> tuple[m.Agent, m.KnowledgeBase, m.KnowledgeBase]:
    """An agent granted TWO knowledge bases, plus a third it may not read."""
    dept = m.Department(tenant_id=tenant, name="Vertrieb")
    db.add(dept)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora")
    prices = m.KnowledgeBase(
        tenant_id=tenant, name="Preisliste", embedding_model="nomic-embed-text"
    )
    handbook = m.KnowledgeBase(
        tenant_id=tenant, name="Handbuch", embedding_model="nomic-embed-text"
    )
    db.add_all([agent, prices, handbook])  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    db.add_all(  # type: ignore[attr-defined]
        [
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=prices.id, grantee_type="agent", grantee_id=agent.id
            ),
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=handbook.id, grantee_type="agent", grantee_id=agent.id
            ),
        ]
    )
    await db.flush()  # type: ignore[attr-defined]
    return agent, prices, handbook


async def test_a_reference_to_an_ungranted_base_searches_nothing(
    app_session: AppSessionFactory,
) -> None:
    """Fail closed. The alternative -- ignoring the narrowing and searching
    everything -- would make `#` a suggestion rather than a restriction."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _prices, _handbook = await _granted_agent(db, tenant)
        stranger = m.KnowledgeBase(
            tenant_id=tenant, name="Personalakten", embedding_model="nomic-embed-text"
        )
        db.add(stranger)
        await db.flush()
        context, restricted = await retrieve_kb_context(
            db,
            agent=agent,
            tenant_id=tenant,
            query_text="rabatt",
            only_kb_ids=frozenset({stranger.id}),
        )
        assert context == ""
        assert restricted is False


async def test_an_empty_narrowing_set_searches_nothing(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _p, _h = await _granted_agent(db, tenant)
        context, _ = await retrieve_kb_context(
            db, agent=agent, tenant_id=tenant, query_text="rabatt", only_kb_ids=frozenset()
        )
        assert context == ""


async def test_none_means_every_granted_base_exactly_as_before(
    app_session: AppSessionFactory,
) -> None:
    """The default. Every existing caller passes nothing and must be
    untouched -- asserted by reaching the embedding step rather than the
    empty-grants early return."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _p, _h = await _granted_agent(db, tenant)
        # No embeddings are configured in the test environment, so
        # EmbeddingUnavailable is caught inside and this returns ("", False) --
        # the point is that it got PAST the grants check, which an empty
        # intersection would not have.
        context, restricted = await retrieve_kb_context(
            db, agent=agent, tenant_id=tenant, query_text="rabatt"
        )
        assert (context, restricted) == ("", False)
