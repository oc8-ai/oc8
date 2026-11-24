"""A chat's own knowledge base: where its large files are made searchable
(spec §5.4).

One hidden base per chat session (`KnowledgeBase.kind == KB_KIND_CHAT`,
`chat_session_id` set), created lazily the first time the chat gets a file
too large to hand to the model inline, and fed by one `DataSource` of its own
(`CHAT_UPLOAD_CONNECTOR`), named in the base's `source_ids`.

Hidden means: never in a knowledge-base list, picker, grant, context search or
an agent's normal retrieval -- each of those filters on `KB_KIND_SHARED`, and
`retrieve_kb_context` searches a chat base only when its caller names it in
`only_kb_ids`. Whoever may read the chat may search its files; no
`knowledge:*` right is involved, so nothing here checks one.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.config import get_settings
from oc8.knowledge.tombstone import tombstone_base, tombstone_source
from oc8.models.knowledge import CHAT_UPLOAD_CONNECTOR, KB_KIND_CHAT


async def _live_chat_kb(db: AsyncSession, session: m.ChatSession) -> m.KnowledgeBase | None:
    return (
        await db.execute(
            select(m.KnowledgeBase).where(
                m.KnowledgeBase.tenant_id == session.tenant_id,
                m.KnowledgeBase.kind == KB_KIND_CHAT,
                m.KnowledgeBase.chat_session_id == session.id,
                m.KnowledgeBase.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def chat_kb_source(db: AsyncSession, *, kb: m.KnowledgeBase) -> m.DataSource | None:
    """The one source that feeds a chat's base, while it is live."""
    for source_id in kb.source_ids or []:
        source = await db.get(m.DataSource, uuid.UUID(str(source_id)))
        if (
            source is not None
            and source.tenant_id == kb.tenant_id
            and source.connector_type == CHAT_UPLOAD_CONNECTOR
            and source.deleted_at is None
        ):
            return source
    return None


async def chat_kb_for(
    db: AsyncSession, *, session: m.ChatSession, create: bool
) -> m.KnowledgeBase | None:
    """The chat's live base, or None. With `create`, one is made when there is
    none: the deployment's default embedding model, `internal`, and a source of
    its own.

    Two uploads racing to create it both succeed: the loser's insert hits
    `uq_knowledge_base_chat_session` inside a savepoint, which is rolled back
    (its source with it), and it returns the winner's base. Flush-only; the
    caller owns the commit."""
    kb = await _live_chat_kb(db, session)
    if kb is not None or not create:
        return kb
    try:
        async with db.begin_nested():
            source = m.DataSource(
                tenant_id=session.tenant_id,
                connector_type=CHAT_UPLOAD_CONNECTOR,
                name="Chat files",
                config={"chat_session_id": str(session.id)},
                classification="internal",
                connected=True,
            )
            db.add(source)
            await db.flush()
            kb = m.KnowledgeBase(
                tenant_id=session.tenant_id,
                name="Chat files",
                description="",
                embedding_model=get_settings().default_embedding_model,
                classification="internal",
                status="current",
                source_ids=[str(source.id)],
                kind=KB_KIND_CHAT,
                chat_session_id=session.id,
            )
            db.add(kb)
            await db.flush()
    except IntegrityError:
        return await _live_chat_kb(db, session)
    return kb


async def drop_chat_kb(db: AsyncSession, *, session: m.ChatSession) -> None:
    """Soft-delete the chat's base and erase its files from it, through the
    same paths an operator's DELETE takes (`knowledge.tombstone`): the content
    is destroyed now, each row stays as its own tombstone, and the removal is
    in the audit chain.

    The source first, then the base: `tombstone_source` takes kb_chunk, then
    data_source, then knowledge_base (its recount), which is that module's lock
    order; `tombstone_base` then finds nothing left to erase and retires the
    base. Done the other way round, a chat whose base holds no chunk yet would
    write knowledge_base before data_source. The audit lock comes at commit,
    after all of these rows (chat/tree.py, step 4). Flush-only."""
    kb = await _live_chat_kb(db, session)
    if kb is None:
        return
    now = dt.datetime.now(tz=dt.UTC)
    source = await chat_kb_source(db, kb=kb)
    if source is not None:
        await tombstone_source(db, tenant_id=session.tenant_id, data_source=source, now=now)
    await tombstone_base(db, tenant_id=session.tenant_id, kb=kb, now=now)
