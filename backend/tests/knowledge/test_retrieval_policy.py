"""Every retrieval path keeps restricted and local-only material off cloud models.

Chat's line: `restricted` content reaches only a local model
(`classification_rule` at retrieval, `ClassificationViolation` in the router),
and a local-only base never leaves the machine. Ingest and the query embedding
for a restricted base already follow it (`test_embedding_policy.py`); these are
the sibling paths next to them, one test per path that used to step over it:

- a hit's label is the strictest of chunk, source and base -- a chunk stored
  before chunks carried their base's or source's label still reads `internal`,
  and a remote index may label its own hits more loosely than the base;
- a local-only base's hits only reach a local model;
- a local-model agent's question is not embedded by a cloud model (what such an
  agent asks may quote restricted material it was given);
- a memory a local-model agent wrote is not recalled into a cloud-model agent;
- restricted text an agent pulls in mid-run with `search_knowledge` marks the
  run restricted, so a later step or a fallback cannot carry it to the cloud.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.knowledge.retrieval import retrieve_kb_context
from oc8.knowledge.vector_indexes.base import VectorHit
from oc8.memory.router import retrieve_context, write_memory
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory
from tests.knowledge.test_embedding_policy import CLOUD, _Http, _kek, _with_key

pytestmark = pytest.mark.asyncio

__all__ = ["_kek"]  # the autouse KEK fixture, re-exported so it applies here too

ALL_CLEARED = {"cleared_classes": ["public", "internal", "confidential", "restricted"]}
SECRET = "salary table for the board"


async def _granted(
    db: Any, tenant: uuid.UUID, *, locality: str | None = None, **kb_kw: Any
) -> tuple[m.Agent, m.KnowledgeBase]:
    kb_kw.setdefault("embedding_model", "nomic-embed-text")
    kb = m.KnowledgeBase(tenant_id=tenant, name="HR", **kb_kw)
    db.add(kb)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
    if locality is not None:
        cfg = m.ModelConfig(
            tenant_id=tenant,
            provider="ollama" if locality == "local" else "anthropic",
            model="llama3" if locality == "local" else "claude",
            locality=locality,
        )
        db.add(cfg)
        await db.flush()
        agent.model_config_id = cfg.id
    db.add(agent)
    await db.flush()
    db.add(
        m.KnowledgeGrant(tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id)
    )
    await db.flush()
    return agent, kb


async def _chunk(
    db: Any,
    tenant: uuid.UUID,
    kb: m.KnowledgeBase,
    *,
    classification: str = "internal",
    data_source_id: uuid.UUID | None = None,
) -> None:
    db.add(
        m.KbChunk(
            tenant_id=tenant,
            kb_id=kb.id,
            content=SECRET,
            source_uri="upload://hr.txt",
            chunk_metadata={},
            classification=classification,
            data_source_id=data_source_id,
            embedding=[0.5] * EMBED_DIM,
        )
    )
    await db.flush()


async def _retrieve(
    db: Any, agent: m.Agent, tenant: uuid.UUID, *, locality: str = "cloud"
) -> tuple[str, bool]:
    return await retrieve_kb_context(
        db,
        agent=agent,
        tenant_id=tenant,
        query_text="salary",
        frame=ALL_CLEARED,
        model_locality=locality,
    )


# ------------------------------------------------- the label a hit carries


async def test_a_chunk_in_a_restricted_base_never_reaches_a_cloud_model_whatever_its_own_label(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chunks stored before ingest copied the base's label read `internal`.
    Retrieval judged only that label, so it handed them to a cloud model."""
    _Http(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, kb = await _granted(db, tenant, classification="restricted")
        await _chunk(db, tenant, kb, classification="internal")
        context, _r = await _retrieve(db, agent, tenant)
    assert SECRET not in context


async def test_a_chunk_of_a_restricted_source_never_reaches_a_cloud_model_whatever_its_own_label(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _Http(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, kb = await _granted(db, tenant)
        source = m.DataSource(
            tenant_id=tenant,
            connector_type="upload",
            name="hr",
            connected=True,
            classification="restricted",
        )
        db.add(source)
        await db.flush()
        await _chunk(db, tenant, kb, classification="internal", data_source_id=source.id)
        context, _r = await _retrieve(db, agent, tenant)
    assert SECRET not in context


async def test_a_remote_index_cannot_label_a_restricted_bases_hits_more_loosely(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote hit's own label used to replace the base's, so `public` from the
    index let a restricted base's text through to a cloud model."""
    _Http(monkeypatch)

    class _Index:
        async def search(self, *a: Any, **kw: Any) -> list[VectorHit]:
            return [
                VectorHit(content=SECRET, source_uri="idx://1", score=1.0, classification="public")
            ]

    async def resolve(*a: Any, **kw: Any) -> _Index:
        return _Index()

    monkeypatch.setattr("oc8.knowledge.retrieval.resolve_vector_index", resolve)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _kb = await _granted(
            db,
            tenant,
            classification="restricted",
            index_type="fake_remote",
            credential_id=uuid.uuid4(),
        )
        context, _r = await _retrieve(db, agent, tenant)
    assert SECRET not in context


# ------------------------------------------------------------- local-only


async def test_a_local_only_bases_hits_reach_only_a_local_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`local_only` used to be carried on every hit and read by nothing, so a
    local-only base's text went to whatever model the agent ran on."""
    _Http(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, kb = await _granted(db, tenant, local_only=True)
        await _chunk(db, tenant, kb)
        cloud_context, _r = await _retrieve(db, agent, tenant, locality="cloud")
        local_context, local_restricted = await _retrieve(db, agent, tenant, locality="local")
    assert SECRET not in cloud_context
    assert SECRET in local_context
    # ...and the run is marked, so its later steps and fallbacks stay local too.
    assert local_restricted is True


# --------------------------------------------------------- the query itself


async def test_a_local_model_agents_question_is_not_embedded_by_a_cloud_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule memory follows: a local-model agent may hold restricted
    material, and the question it searches with is derived from it."""
    http = _Http(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _with_key(db, tenant)
        agent, kb = await _granted(db, tenant, locality="local", embedding_model=CLOUD)
        await _chunk(db, tenant, kb)
        await _retrieve(db, agent, tenant, locality="local")
    assert http.cloud_calls == 0


async def test_a_cloud_model_agents_question_still_uses_a_cloud_bases_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is about where the agent runs, not a blanket ban: an ordinary
    base with a cloud model keeps working for a cloud-model agent."""
    http = _Http(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _with_key(db, tenant)
        agent, kb = await _granted(db, tenant, locality="cloud", embedding_model=CLOUD)
        await _chunk(db, tenant, kb)
        context, _r = await _retrieve(db, agent, tenant, locality="cloud")
    assert http.cloud_calls == 1
    assert SECRET in context


# ------------------------------------------------------------------- memory


async def test_a_local_model_agents_shared_memory_is_not_recalled_into_a_cloud_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A department memory written by a local-model agent may quote restricted
    material. A cloud-model colleague in the same department recalled it and
    sent it to its provider."""
    _Http(monkeypatch)
    tenant = uuid.uuid4()
    frame = {"memory": {"department": ["read", "write"]}}
    async with app_session(tenant) as db:
        local_cfg = m.ModelConfig(
            tenant_id=tenant, provider="ollama", model="llama3", locality="local"
        )
        cloud_cfg = m.ModelConfig(
            tenant_id=tenant, provider="anthropic", model="claude", locality="cloud"
        )
        db.add_all([local_cfg, cloud_cfg])
        await db.flush()
        dept = uuid.uuid4()
        local_agent = m.Agent(
            tenant_id=tenant, department_id=dept, name="L", model_config_id=local_cfg.id
        )
        cloud_agent = m.Agent(
            tenant_id=tenant, department_id=dept, name="C", model_config_id=cloud_cfg.id
        )
        db.add_all([local_agent, cloud_agent])
        await db.flush()
        await write_memory(
            db, tenant_id=tenant, agent=local_agent, tier="department", content=SECRET
        )
        await write_memory(
            db, tenant_id=tenant, agent=cloud_agent, tier="department", content="opening hours 9-17"
        )
        cloud_recall = await retrieve_context(
            db, agent=cloud_agent, tenant_id=tenant, frame=frame, query_text="salary"
        )
        local_recall = await retrieve_context(
            db, agent=local_agent, tenant_id=tenant, frame=frame, query_text="salary"
        )
    assert SECRET not in cloud_recall
    assert "opening hours" in cloud_recall
    assert SECRET in local_recall


# --------------------------------------------- restricted text pulled mid-run


async def test_search_knowledge_reports_restricted_text_to_its_caller(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool threw retrieval's `contains_restricted` away. That flag is what
    makes the router refuse a cloud model (and a cloud fallback) and keeps the
    answer out of the department cache, so restricted text fetched mid-run on a
    local model could leave through the next step's fallback."""
    from oc8.agent.control_tools import SEARCH_KNOWLEDGE, execute_control_tool
    from oc8.authz.pdp import Decision, Effect
    from oc8.modelrouter import ToolCall

    async def fake_retrieve(*a: Any, **kw: Any) -> tuple[str, bool]:
        return SECRET, True

    monkeypatch.setattr("oc8.agent.control_tools.retrieve_kb_context", fake_retrieve)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        task = m.Task(
            tenant_id=tenant,
            department_id=agent.department_id,
            assigned_agent_id=agent.id,
            title="t",
            state="in_progress",
        )
        db.add(task)
        await db.flush()
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="c1", name=SEARCH_KNOWLEDGE.name, arguments={"query": "salary"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
    assert outcome is not None
    assert outcome.contains_restricted is True
