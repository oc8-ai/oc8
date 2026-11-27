"""chat_harness / knowledge: questions against a 200-chunk corpus whose
identifiers differ by a digit or two (phase 1b §5.6 hybrid retrieval).

`setup` builds one knowledge base for the chat's agent: articles and invoices
with near-duplicate numbers (ART-10427 / ART-10472 / ART-14027 ...,
RE-2026-004711 / RE-2026-004717 ...), each chunk under its own source uri,
embedded with the base's embedding model. Scored on state: the stored answer
(`params.expect` / `params.forbid`), and -- reported, not scored -- where the
harness's own retrieval (`retrieve_kb_context`, the call the turn's preamble
makes) ranks `params.target` for the turn's text: `hit@1`, `hit@5`.
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
from typing import Any

from oc8 import models as m
from oc8.config import get_settings
from oc8.db.session import tenant_session
from oc8.knowledge.retrieval import retrieve_kb_context
from oc8.modelrouter.router import get_model_router
from oc8_evals.scenarios import _chat
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext

#: The documents the questions are about, and their closest look-alikes.
ANCHORS: dict[str, str] = {
    "ART-10427": "Article ART-10427: blue stainless thermos bottle 0.75 l, list price 38.90 EUR.",
    "ART-10472": "Article ART-10472: blue stainless thermos bottle 0.5 l, list price 34.50 EUR.",
    "ART-14027": "Article ART-14027: green stainless thermos bottle 0.75 l, list price 41.20 EUR.",
    "ART-10247": "Article ART-10247: blue enamel camping mug 0.4 l, list price 12.80 EUR.",
    "RE-2026-004711": "Invoice RE-2026-004711 for Nordhafen Logistik was paid on 3 March 2026.",
    "RE-2026-004717": "Invoice RE-2026-004717 for Nordhafen Logistik was paid on 17 March 2026.",
    "RE-2026-007411": "Invoice RE-2026-007411 for Weserbogen GmbH is due on 30 April 2026.",
    "RE-2025-004711": "Invoice RE-2025-004711 for Nordhafen Logistik was paid on 9 March 2025.",
}
CORPUS_SIZE = 200
_COLOURS = ("blue", "green", "red", "black", "white", "grey")
_MATERIALS = ("stainless", "enamel", "glass", "bamboo", "ceramic")
_ITEMS = ("thermos bottle", "camping mug", "lunch box", "water bottle", "travel cup")
_SIZES = ("0.3 l", "0.4 l", "0.5 l", "0.75 l", "1 l")
_TOWNS = ("Bremen", "Kassel", "Lübeck", "Gießen")
_SOURCE = re.compile(r"\(source: ([^)]+)\)")


def _near_numbers(seed: str) -> list[str]:
    """Every other ordering of `seed`'s digits: the look-alikes of an id."""
    return sorted({"".join(p) for p in itertools.permutations(seed)} - {seed})


def corpus() -> dict[str, str]:
    """doc id -> chunk text: the anchors, then generated articles and invoices
    (near-duplicates of the anchors first) up to CORPUS_SIZE."""
    docs = dict(ANCHORS)
    article_ids = [f"ART-{n}" for n in _near_numbers("10427")]
    invoice_ids = [f"RE-2026-{n}" for n in _near_numbers("004711")]
    n = 0
    for doc_id in itertools.chain(*itertools.zip_longest(article_ids, invoice_ids)):
        if len(docs) >= CORPUS_SIZE:
            break
        if doc_id is None or doc_id in docs:
            continue
        n += 1
        if doc_id.startswith("ART-"):
            price = f"{10 + (n * 37) % 90}.{(n * 13) % 100:02d}"
            docs[doc_id] = (
                f"Article {doc_id}: {_COLOURS[n % 6]} {_MATERIALS[n % 5]} {_ITEMS[n % 5]} "
                f"{_SIZES[(n // 5) % 5]}, list price {price} EUR, warehouse {_TOWNS[n % 4]}."
            )
        else:
            docs[doc_id] = (
                f"Invoice {doc_id} for customer {n:03d} was paid on {1 + n % 27} "
                f"{('January', 'February', 'May', 'June')[n % 4]} 2026."
            )
    return docs


def _uri(ctx: ScenarioContext, doc_id: str) -> str:
    return f"eval://{ctx.prefix}/{doc_id}"


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    fixture = _chat.chat(ctx)
    router = get_model_router()
    async with tenant_session(ctx.tenant_id) as db:
        kb = m.KnowledgeBase(
            tenant_id=ctx.tenant_id,
            name=f"EVAL {ctx.prefix} catalogue",
            embedding_model=get_settings().default_embedding_model,
        )
        db.add(kb)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=ctx.tenant_id,
                kb_id=kb.id,
                grantee_type="agent",
                grantee_id=fixture.agent_id,
            )
        )
        for doc_id, content in corpus().items():
            embedding = await router.embed(
                content, model=kb.embedding_model, tenant_id=ctx.tenant_id, db=db
            )
            db.add(
                m.KbChunk(
                    tenant_id=ctx.tenant_id,
                    kb_id=kb.id,
                    content=content,
                    source_uri=_uri(ctx, doc_id),
                    chunk_metadata={},
                    classification="internal",
                    embedding=embedding,
                )
            )
        await db.flush()
        return {"kb_id": kb.id}


async def _rank(ctx: ScenarioContext, target: str) -> int | None:
    """Where the turn's retrieval puts `target` (1 = first), None when absent."""
    fixture = _chat.chat(ctx)
    async with tenant_session(ctx.tenant_id) as db:
        agent = await db.get(m.Agent, fixture.agent_id)
        dept = await db.get(m.Department, fixture.department_id) if fixture.department_id else None
        assert agent is not None
        context, _restricted = await retrieve_kb_context(
            db,
            agent=agent,
            tenant_id=ctx.tenant_id,
            query_text=ctx.task.task_text,
            frame=(dept.frame if dept is not None else {}) or {},
            model_locality=await _chat.model_locality(ctx) or "cloud",
        )
    order = list(dict.fromkeys(_SOURCE.findall(context)))
    uri = _uri(ctx, target)
    return order.index(uri) + 1 if uri in order else None


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    params = ctx.task.params
    target = params.get("target")
    if target:
        rank = await _rank(ctx, str(target))
        ctx.metrics.update(
            {
                "hit@1": 1.0 if rank == 1 else 0.0,
                "hit@5": 1.0 if rank is not None and rank <= 5 else 0.0,
            }
        )
    text = await _chat.reply_text(ctx)
    found, _ = _chat.reply_checks(text, expect=list(params.get("expect", [])), forbid=[])
    return [_chat.answered(ctx, text), *found]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    text = await _chat.reply_text(ctx)
    _, bad = _chat.reply_checks(text, expect=[], forbid=list(ctx.task.params.get("forbid", [])))
    return bad


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    """The base is soft-deleted, so the tenant's other agents never see it."""
    kb_id = seeded.get("kb_id")
    if kb_id is None:
        return
    async with tenant_session(ctx.tenant_id) as db:
        kb = await db.get(m.KnowledgeBase, kb_id)
        if kb is not None:
            kb.deleted_at = dt.datetime.now(dt.UTC)
