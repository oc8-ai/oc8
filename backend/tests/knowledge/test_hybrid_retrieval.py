"""Hybrid retrieval (phase 1b §5.6): full-text search fused into vector search.

Cosine similarity over embeddings finds what a passage is about, not the exact
string in it, so an invoice number or an article code the question names
verbatim can lose to fifty passages that merely sound alike. Each internal base
is therefore searched twice -- top 50 by cosine, top 50 by
`websearch_to_tsquery('simple', q)` -- and the lists are fused with reciprocal
rank fusion (k = 60) before the classification filter and the token budget.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.knowledge import retrieval
from oc8.knowledge.retrieval import RRF_K, _Hit, fuse_rrf, retrieve_kb_context
from oc8.knowledge.vector_indexes.base import VectorHit
from oc8.models.knowledge import EMBED_DIM, KB_KIND_CHAT
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

INVOICE = "RE-2026-004711"
TARGET = f"Invoice {INVOICE} was paid by bank transfer on 3 March"
ALL_CLEARED = {"cleared_classes": ["public", "internal", "confidential", "restricted"]}


def _unit(axis: int, tilt: float = 0.0) -> list[float]:
    """A vector along `axis`, tilted by `tilt` toward the last axis, so that
    smaller tilts sit closer to the query."""
    vec = [0.0] * EMBED_DIM
    vec[axis] = 1.0
    vec[EMBED_DIM - 1] = tilt
    return vec


#: Every question embeds to the first axis.
QUERY_VECTOR = _unit(0)


class _AxisRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        return list(QUERY_VECTOR)


@pytest.fixture(autouse=True)
def _router(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _AxisRouter())


async def _granted_kb(db: Any, tenant: uuid.UUID, **kb_kw: Any) -> tuple[m.Agent, m.KnowledgeBase]:
    kb_kw.setdefault("embedding_model", "nomic-embed-text")
    kb = m.KnowledgeBase(tenant_id=tenant, name=kb_kw.pop("name", "Invoices"), **kb_kw)
    db.add(kb)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
    db.add(agent)
    await db.flush()
    db.add(
        m.KnowledgeGrant(tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id)
    )
    await db.flush()
    return agent, kb


def _chunk(
    tenant: uuid.UUID,
    kb: m.KnowledgeBase,
    content: str,
    embedding: list[float],
    *,
    source_uri: str = "upload://invoices.txt",
    classification: str = "internal",
) -> m.KbChunk:
    return m.KbChunk(
        tenant_id=tenant,
        kb_id=kb.id,
        content=content,
        source_uri=source_uri,
        chunk_metadata={},
        classification=classification,
        embedding=embedding,
    )


async def _seed_decoys(db: Any, tenant: uuid.UUID, kb: m.KnowledgeBase, n: int = 60) -> None:
    """`n` chunks every one of which sits closer to the question than the
    target does, so cosine alone fills its 50 candidates with them."""
    for i in range(n):
        db.add(_chunk(tenant, kb, f"decoy note {i} about the weather", _unit(0, 0.01 * (i + 1))))
    await db.flush()


async def _retrieve(
    db: Any, agent: m.Agent, tenant: uuid.UUID, q: str, **kw: Any
) -> tuple[str, bool]:
    kw.setdefault("frame", ALL_CLEARED)
    return await retrieve_kb_context(db, agent=agent, tenant_id=tenant, query_text=q, **kw)


def _vector_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cosine only, as before hybrid retrieval: fuse each base's vector list
    (the first of its two) and drop the full-text list."""
    real = retrieval.fuse_rrf

    def first_list_only(ranked_lists: list[list[_Hit]], **kw: Any) -> list[_Hit]:
        return real(ranked_lists[:1], **kw)

    monkeypatch.setattr("oc8.knowledge.retrieval.fuse_rrf", first_list_only)


# ------------------------------------------------------------ exact terms


async def test_an_invoice_number_is_found_by_full_text_that_cosine_misses(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, kb = await _granted_kb(db, tenant)
        await _seed_decoys(db, tenant, kb)
        db.add(_chunk(tenant, kb, TARGET, _unit(1)))
        await db.flush()

        hybrid, _ = await _retrieve(db, agent, tenant, INVOICE)
        with monkeypatch.context() as mp:
            _vector_only(mp)
            cosine_only, _ = await _retrieve(db, agent, tenant, INVOICE)

    assert TARGET in hybrid
    assert TARGET not in cosine_only
    assert "decoy note 0 about the weather" in cosine_only


# ------------------------------------------------------------------- fusion


def _hit(name: str, **kw: Any) -> _Hit:
    kw.setdefault("chunk_id", uuid.uuid5(uuid.NAMESPACE_OID, name))
    return _Hit(
        kb_id=kw.pop("kb_id", uuid.UUID(int=1)),
        content=name,
        source_uri=kw.pop("source_uri", f"upload://{name}"),
        classification="internal",
        score=kw.pop("score", 0.0),
        **kw,
    )


async def test_rrf_order() -> None:
    a, b, c, d = (_hit(x) for x in "ABCD")
    vector = [a, b, c]
    text = [_hit("C"), _hit("D"), _hit("A")]

    fused = fuse_rrf([vector, text])

    assert RRF_K == 60
    # A and C both score 1/61 + 1/63, B and D both 1/62: each tie goes to the
    # hit the vector list ranked higher (D is not in it at all).
    assert [h.content for h in fused] == ["A", "C", "B", "D"]
    assert fused[0].rank_score == pytest.approx(1 / 61 + 1 / 63)
    assert fused[2].rank_score == pytest.approx(1 / 62)
    assert fused[3].rank_score == pytest.approx(1 / 62)
    # One hit per chunk, and its cosine score is kept.
    assert len({h.chunk_id for h in fused}) == 4
    assert fused[0] is a
    assert d.chunk_id == fused[3].chunk_id


async def test_rrf_identifies_an_external_hit_by_base_source_and_content() -> None:
    kb = uuid.uuid4()
    same = _hit("same text", kb_id=kb, source_uri="idx://1", chunk_id=None)
    again = _hit("same text", kb_id=kb, source_uri="idx://1", chunk_id=None)
    other_source = _hit("same text", kb_id=kb, source_uri="idx://2", chunk_id=None)

    fused = fuse_rrf([[same, other_source], [again]])

    assert len(fused) == 2
    assert fused[0] is same
    assert fused[0].rank_score == pytest.approx(2 / 61)


@pytest.mark.parametrize("query", ["#", "--", '"', ""])
async def test_a_query_without_words_falls_back_to_vector_only(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, kb = await _granted_kb(db, tenant)
        await _seed_decoys(db, tenant, kb, n=5)
        db.add(_chunk(tenant, kb, TARGET, _unit(1)))
        await db.flush()

        hybrid = await _retrieve(db, agent, tenant, query)
        with monkeypatch.context() as mp:
            _vector_only(mp)
            vector_only = await _retrieve(db, agent, tenant, query)

    assert hybrid == vector_only
    assert "decoy note 0 about the weather" in hybrid[0]


# -------------------------------------------------- filters stay where they are


async def test_restricted_and_local_only_filtering_unchanged(
    app_session: AppSessionFactory,
) -> None:
    """A chunk only full-text finds is still judged by the strictest label of
    chunk, source and base, and by the base's `local_only`, and what reaches a
    local model still marks the run restricted."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, kb = await _granted_kb(db, tenant)
        await _seed_decoys(db, tenant, kb)
        db.add(_chunk(tenant, kb, TARGET, _unit(1), classification="restricted"))
        _agent2, local_kb = await _granted_kb(db, tenant, name="Local", local_only=True)
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=local_kb.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        local_text = f"Credit note for {INVOICE} kept on premises"
        db.add(_chunk(tenant, local_kb, local_text, _unit(1)))
        await db.flush()

        cloud_ctx, cloud_restricted = await _retrieve(
            db, agent, tenant, INVOICE, model_locality="cloud"
        )
        local_ctx, local_restricted = await _retrieve(
            db, agent, tenant, INVOICE, model_locality="local"
        )
        no_clearance_ctx, _ = await _retrieve(
            db, agent, tenant, INVOICE, model_locality="local", frame={}
        )

    assert TARGET not in cloud_ctx
    assert local_text not in cloud_ctx
    assert cloud_restricted is False
    assert TARGET in local_ctx
    assert local_text in local_ctx
    assert local_restricted is True
    # Without clearance for restricted, a local model does not get it either.
    assert TARGET not in no_clearance_ctx


async def test_chat_kb_full_text_respects_only_sources(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        kb = m.KnowledgeBase(
            tenant_id=tenant,
            name="Chat files",
            embedding_model="nomic-embed-text",
            kind=KB_KIND_CHAT,
            chat_session_id=uuid.uuid4(),
        )
        db.add(kb)
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        await _seed_decoys(db, tenant, kb)
        db.add(_chunk(tenant, kb, TARGET, _unit(1), source_uri="chat-file://this"))
        other = f"Reminder for {INVOICE} in another file"
        db.add(_chunk(tenant, kb, other, _unit(1), source_uri="chat-file://other"))
        await db.flush()

        ctx, _ = await _retrieve(
            db,
            agent,
            tenant,
            INVOICE,
            only_kb_ids=frozenset({kb.id}),
            released_kb_ids=frozenset({kb.id}),
            only_sources={"chat-file://this": "invoices.pdf"},
        )

    assert TARGET in ctx
    assert "(source: invoices.pdf)" in ctx
    assert other not in ctx
    assert "decoy note" not in ctx


async def test_external_index_is_fused_but_not_text_searched(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An external base gets its single remote list fused with the internal
    lists; oc8 never runs its own full-text query against it."""
    remote_calls: list[dict[str, Any]] = []

    class _Index:
        async def search(self, *a: Any, **kw: Any) -> list[VectorHit]:
            remote_calls.append(kw)
            return [VectorHit(content="remote passage", source_uri="idx://1", score=0.9)]

    async def resolve(*a: Any, **kw: Any) -> _Index:
        return _Index()

    monkeypatch.setattr("oc8.knowledge.retrieval.resolve_vector_index", resolve)

    text_searched: list[uuid.UUID] = []
    real_internal = retrieval._search_internal

    async def spy_internal(db: Any, **kw: Any) -> Any:
        text_searched.append(kw["kb"].id)
        return await real_internal(db, **kw)

    monkeypatch.setattr("oc8.knowledge.retrieval._search_internal", spy_internal)

    fused_inputs: list[list[list[_Hit]]] = []
    real_fuse = retrieval.fuse_rrf

    def spy_fuse(ranked_lists: list[list[_Hit]], **kw: Any) -> list[_Hit]:
        fused_inputs.append(ranked_lists)
        return real_fuse(ranked_lists, **kw)

    monkeypatch.setattr("oc8.knowledge.retrieval.fuse_rrf", spy_fuse)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, internal = await _granted_kb(db, tenant)
        db.add(_chunk(tenant, internal, TARGET, _unit(1)))
        _a2, external = await _granted_kb(
            db, tenant, name="Remote", index_type="fake_remote", credential_id=uuid.uuid4()
        )
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=external.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        await db.flush()

        ctx, _ = await _retrieve(db, agent, tenant, INVOICE)

    assert "remote passage" in ctx
    assert TARGET in ctx
    assert len(remote_calls) == 1
    assert text_searched == [internal.id]
    # One fusion over every list: the internal base's two and the external one.
    assert len(fused_inputs) == 1
    assert sorted(len(lst) for lst in fused_inputs[0]) == [1, 1, 1]
