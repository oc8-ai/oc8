"""Knowledge bases, data sources, a file inside a source, and chat/run files as
chat context.

Each `authorize` is the rule oc8 already applies to that object:

* knowledge base, data source, source file -- `knowledge:view`, held the way
  `require_permission` reads it on every `/knowledge/*` read route. A source
  file is exactly as visible as its source. There is no per-base or
  per-department visibility for a person: `KnowledgeGrant` gives a base to an
  AGENT's retrieval, not to a member.
* file attachment -- `api/v1/files.py::_owned_attachment`: a chat file to the
  owner of its session (the only member who can upload into it) or to the
  session's audience; a run output or instruction file to whoever may see the
  agent it belongs to.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.repo import visible_agent
from oc8.authz.authority import tenant_wide_read
from oc8.authz.permissions import AGENT, KNOWLEDGE, VIEW, perm
from oc8.chat.audience import session_audience
from oc8.chat.context.base import ContextPage, Viewer, paginate
from oc8.chat.context.registry import register
from oc8.knowledge import browse
from oc8.knowledge.embedding_policy import strictest

#: What an object with no classification of its own is treated as: the
#: column default of `KnowledgeBase.classification` and `DataSource.classification`.
DEFAULT_CLASSIFICATION = "internal"


def _may_view_knowledge(viewer: Viewer) -> bool:
    return perm(KNOWLEDGE, VIEW) in viewer.authority.tenant_wide


async def _live_source(db: AsyncSession, tenant_id: Any, ref_id: str) -> m.DataSource | None:
    return (
        await db.execute(
            select(m.DataSource).where(
                m.DataSource.id == uuid.UUID(ref_id),
                m.DataSource.tenant_id == tenant_id,
                m.DataSource.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


class KnowledgeBaseResolver:
    kind = "knowledge_base"
    readable = False

    async def load(self, db: AsyncSession, tenant_id: Any, ref_id: str) -> Any | None:
        return (
            await db.execute(
                select(m.KnowledgeBase).where(
                    m.KnowledgeBase.id == uuid.UUID(ref_id),
                    m.KnowledgeBase.tenant_id == tenant_id,
                    m.KnowledgeBase.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    async def authorize(self, db: AsyncSession, viewer: Viewer, obj: Any) -> bool:
        return _may_view_knowledge(viewer)

    def label(self, obj: Any) -> str:
        return str(obj.name)

    def classification(self, obj: Any) -> str:
        # A local-only base is held to the same line as `restricted` content:
        # it never reaches a cloud model (`knowledge/embedding_policy.py`).
        return strictest(obj.classification, "restricted" if obj.local_only else None)

    async def summary(self, db: AsyncSession, obj: Any) -> str:
        return str(obj.description or "")

    def deep_link(self, obj: Any) -> str | None:
        return f"/knowledge/bases/{obj.id}/content"

    async def read(self, db: AsyncSession, obj: Any, page: int) -> ContextPage | None:
        return None

    async def search(self, db: AsyncSession, viewer: Viewer, query: str, limit: int) -> list[Any]:
        if not _may_view_knowledge(viewer):
            return []
        rows = await db.execute(
            select(m.KnowledgeBase)
            .where(
                m.KnowledgeBase.tenant_id == viewer.member.tenant_id,
                m.KnowledgeBase.deleted_at.is_(None),
                m.KnowledgeBase.name.ilike(f"%{query}%"),
            )
            .order_by(m.KnowledgeBase.name)
            .limit(limit)
        )
        return list(rows.scalars())


class DataSourceResolver:
    kind = "data_source"
    readable = False

    async def load(self, db: AsyncSession, tenant_id: Any, ref_id: str) -> Any | None:
        return await _live_source(db, tenant_id, ref_id)

    async def authorize(self, db: AsyncSession, viewer: Viewer, obj: Any) -> bool:
        return _may_view_knowledge(viewer)

    def label(self, obj: Any) -> str:
        return str(obj.name)

    def classification(self, obj: Any) -> str:
        return strictest(obj.classification)

    async def summary(self, db: AsyncSession, obj: Any) -> str:
        return f"{obj.doc_count} documents"

    def deep_link(self, obj: Any) -> str | None:
        return "/knowledge"

    async def read(self, db: AsyncSession, obj: Any, page: int) -> ContextPage | None:
        return None

    async def search(self, db: AsyncSession, viewer: Viewer, query: str, limit: int) -> list[Any]:
        if not _may_view_knowledge(viewer):
            return []
        rows = await db.execute(
            select(m.DataSource)
            .where(
                m.DataSource.tenant_id == viewer.member.tenant_id,
                m.DataSource.deleted_at.is_(None),
                m.DataSource.name.ilike(f"%{query}%"),
            )
            .order_by(m.DataSource.name)
            .limit(limit)
        )
        return list(rows.scalars())


class SourceFileResolver:
    """One file inside a data source. `id` is `"<data_source_id>:<file_id>"`;
    the connector's own file id is everything after the FIRST colon and may
    itself contain colons. The object is the pair `(source, file_id)`."""

    kind = "source_file"
    readable = True

    async def load(self, db: AsyncSession, tenant_id: Any, ref_id: str) -> Any | None:
        source_id, _, file_id = ref_id.partition(":")
        if not file_id:
            return None
        source = await _live_source(db, tenant_id, source_id)
        return (source, file_id) if source is not None else None

    async def authorize(self, db: AsyncSession, viewer: Viewer, obj: Any) -> bool:
        return _may_view_knowledge(viewer)

    def label(self, obj: Any) -> str:
        return str(obj[1]).rstrip("/").rsplit("/", 1)[-1] or str(obj[1])

    def classification(self, obj: Any) -> str:
        return strictest(obj[0].classification)

    async def summary(self, db: AsyncSession, obj: Any) -> str:
        return f"File in {obj[0].name}"

    def deep_link(self, obj: Any) -> str | None:
        return None

    async def read(self, db: AsyncSession, obj: Any, page: int) -> ContextPage | None:
        """None when the source's connector cannot fetch single files. A
        `ConnectorError` (too large, unreachable, invalid id) is raised so the
        caller can say why; its message never carries connector internals."""
        try:
            fetched = await browse.fetch_source_file(db, source=obj[0], file_id=obj[1])
        except browse.BrowseUnsupported:
            return None
        return paginate(fetched.text, page)

    async def search(self, db: AsyncSession, viewer: Viewer, query: str, limit: int) -> list[Any]:
        # A source's files are found by browsing it, not by a name search.
        return []


async def _chat_session_of(
    db: AsyncSession, tenant_id: uuid.UUID, owner_id: uuid.UUID
) -> m.ChatSession | None:
    """A chat file's session. `owner_id` is the session until the message
    carrying the file is sent and the message from then on (`files.py`)."""
    session = await _session(db, tenant_id, owner_id)
    if session is not None:
        return session
    message = (
        await db.execute(
            select(m.ChatMessage).where(
                m.ChatMessage.id == owner_id, m.ChatMessage.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if message is None:
        return None
    return await _session(db, tenant_id, message.session_id)


async def _session(
    db: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> m.ChatSession | None:
    return (
        await db.execute(
            select(m.ChatSession).where(
                m.ChatSession.id == session_id, m.ChatSession.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()


class FileResolver:
    """A `FileAttachment`: a chat upload, a run's output, or an agent's
    instruction file."""

    kind = "file"
    readable = True

    async def load(self, db: AsyncSession, tenant_id: Any, ref_id: str) -> Any | None:
        return (
            await db.execute(
                select(m.FileAttachment).where(
                    m.FileAttachment.id == uuid.UUID(ref_id),
                    m.FileAttachment.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def authorize(self, db: AsyncSession, viewer: Viewer, obj: Any) -> bool:
        tenant_id = viewer.member.tenant_id
        if obj.owner_type == "chat_message":
            session = await _chat_session_of(db, tenant_id, obj.owner_id)
            if session is None:
                return False
            if session.member_id == viewer.member.id:
                return True
            return viewer.member.id in await session_audience(db, session=session)
        if obj.owner_type in ("agent_run", "agent_instructions"):
            agent_id = obj.owner_id
            if obj.owner_type == "agent_run":
                run = (
                    await db.execute(
                        select(m.AgentRun).where(
                            m.AgentRun.id == obj.owner_id, m.AgentRun.tenant_id == tenant_id
                        )
                    )
                ).scalar_one_or_none()
                if run is None:
                    return False
                agent_id = run.agent_id
            agent = await visible_agent(
                db,
                scope=viewer.scope,
                tenant_wide=tenant_wide_read(viewer.authority, perm(AGENT, VIEW)),
                agent_id=agent_id,
            )
            return agent is not None
        return False

    def label(self, obj: Any) -> str:
        return str(obj.filename)

    def classification(self, obj: Any) -> str:
        return DEFAULT_CLASSIFICATION

    async def summary(self, db: AsyncSession, obj: Any) -> str:
        return f"{obj.content_type}, {obj.size_bytes} bytes"

    def deep_link(self, obj: Any) -> str | None:
        return None

    async def read(self, db: AsyncSession, obj: Any, page: int) -> ContextPage | None:
        return paginate(obj.extracted_text or "", page)

    async def search(self, db: AsyncSession, viewer: Viewer, query: str, limit: int) -> list[Any]:
        """Files from the viewer's own chats, newest first."""
        sessions = select(m.ChatSession.id).where(
            m.ChatSession.tenant_id == viewer.member.tenant_id,
            m.ChatSession.member_id == viewer.member.id,
        )
        messages = select(m.ChatMessage.id).where(m.ChatMessage.session_id.in_(sessions))
        rows = await db.execute(
            select(m.FileAttachment)
            .where(
                m.FileAttachment.tenant_id == viewer.member.tenant_id,
                m.FileAttachment.owner_type == "chat_message",
                or_(
                    m.FileAttachment.owner_id.in_(messages),
                    m.FileAttachment.owner_id.in_(sessions),
                ),
                m.FileAttachment.filename.ilike(f"%{query}%"),
            )
            .order_by(m.FileAttachment.created_at.desc())
            .limit(limit)
        )
        return list(rows.scalars())


for _resolver in (
    KnowledgeBaseResolver(),
    DataSourceResolver(),
    SourceFileResolver(),
    FileResolver(),
):
    register(_resolver)
