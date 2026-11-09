"""Knowledge bases, data sources, a file inside a source, and chat/run files as
chat context.

Each check is the rule oc8 already applies to that object:

* knowledge base, data source, source file -- `knowledge:view`, held the way
  `require_permission` reads it on every `/knowledge/*` read route, and checked
  before anything is loaded. A source file is exactly as visible as its
  source, and only a file the source's connector confirms lies inside the
  source's own scope can be pinned or read (`browse.source_file_in_scope`).
  There is no per-base or per-department visibility for a person:
  `KnowledgeGrant` gives a base to an AGENT's retrieval, not to a member.
* file attachment -- `storage.attachment_access.member_may_open_attachment`,
  the predicate `GET /files/{id}` itself uses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.permissions import KNOWLEDGE, VIEW, perm
from oc8.chat.context.base import ContextPage, Viewer, paginate
from oc8.chat.context.registry import register
from oc8.knowledge import browse
from oc8.knowledge.connectors.base import ConnectorError
from oc8.knowledge.embedding_policy import strictest
from oc8.storage.attachment_access import member_may_open_attachment, passes_files_door

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


async def _source_classification(db: AsyncSession, source: m.DataSource) -> str:
    """The strictest of the source's own label and that of every live base it
    feeds, with a local-only base counting as `restricted` -- the label its
    content carries once ingested (`knowledge/ingest.py`), so reading the raw
    file is held to the same line as reading its chunks."""
    bases = (
        await db.execute(
            select(m.KnowledgeBase.classification, m.KnowledgeBase.local_only).where(
                m.KnowledgeBase.tenant_id == source.tenant_id,
                m.KnowledgeBase.deleted_at.is_(None),
                m.KnowledgeBase.source_ids.contains([str(source.id)]),
            )
        )
    ).all()
    return strictest(
        source.classification,
        *[label for label, _local in bases],
        "restricted" if any(local for _label, local in bases) else None,
    )


class _KnowledgeKind:
    """Shared by the three knowledge kinds: the gate is `knowledge:view`."""

    def admits(self, viewer: Viewer) -> bool:
        return _may_view_knowledge(viewer)

    async def authorize(self, db: AsyncSession, viewer: Viewer, obj: Any) -> bool:
        return _may_view_knowledge(viewer)


class KnowledgeBaseResolver(_KnowledgeKind):
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

    def ref_id(self, obj: Any) -> str:
        return str(obj.id)

    def label(self, obj: Any) -> str:
        return str(obj.name)

    async def classification(self, db: AsyncSession, obj: Any) -> str:
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


class DataSourceResolver(_KnowledgeKind):
    kind = "data_source"
    readable = False

    async def load(self, db: AsyncSession, tenant_id: Any, ref_id: str) -> Any | None:
        return await _live_source(db, tenant_id, ref_id)

    def ref_id(self, obj: Any) -> str:
        return str(obj.id)

    def label(self, obj: Any) -> str:
        return str(obj.name)

    async def classification(self, db: AsyncSession, obj: Any) -> str:
        return await _source_classification(db, obj)

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


@dataclass(frozen=True)
class SourceFile:
    source: m.DataSource
    file_id: str


class SourceFileResolver(_KnowledgeKind):
    """One file inside a data source. `id` is `"<data_source_id>:<file_id>"`;
    the connector's own file id is everything after the FIRST colon and may
    itself contain colons.

    `load` accepts a file only when the source's connector confirms it lies
    inside the source's configured scope (`BrowsingConnector.in_scope`): a
    pinned id is untrusted, and the connection behind a source usually reaches
    much more than the source was set up to share."""

    kind = "source_file"
    readable = True

    async def load(self, db: AsyncSession, tenant_id: Any, ref_id: str) -> Any | None:
        source_id, _, file_id = ref_id.partition(":")
        try:
            file_id = browse.check_file_id(file_id)
        except ConnectorError:
            return None
        source = await _live_source(db, tenant_id, source_id)
        if source is None:
            return None
        if not await browse.source_file_in_scope(db, source=source, file_id=file_id):
            return None
        return SourceFile(source=source, file_id=file_id)

    def ref_id(self, obj: Any) -> str:
        return f"{obj.source.id}:{obj.file_id}"

    def label(self, obj: Any) -> str:
        return str(obj.file_id).rstrip("/").rsplit("/", 1)[-1] or str(obj.file_id)

    async def classification(self, db: AsyncSession, obj: Any) -> str:
        return await _source_classification(db, obj.source)

    async def summary(self, db: AsyncSession, obj: Any) -> str:
        return f"File in {obj.source.name}"

    def deep_link(self, obj: Any) -> str | None:
        return None

    async def read(self, db: AsyncSession, obj: Any, page: int) -> ContextPage | None:
        """None when the source's connector cannot fetch single files. A
        `ConnectorError` (too large, out of scope, unreachable) is raised so
        the caller can say why; its message never carries connector internals."""
        try:
            fetched = await browse.fetch_source_file(db, source=obj.source, file_id=obj.file_id)
        except browse.BrowseUnsupported:
            return None
        return paginate(fetched.text, page)

    async def search(self, db: AsyncSession, viewer: Viewer, query: str, limit: int) -> list[Any]:
        # A source's files are found by browsing it, not by a name search.
        return []


class FileResolver:
    """A `FileAttachment`: a chat upload, a run's output, or an agent's
    instruction file -- visible exactly when `GET /files/{id}` would serve it."""

    kind = "file"
    readable = True

    def admits(self, viewer: Viewer) -> bool:
        return passes_files_door(viewer.authority, viewer.scope)

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
        return await member_may_open_attachment(
            db, member=viewer.member, authority=viewer.authority, scope=viewer.scope, row=obj
        )

    def ref_id(self, obj: Any) -> str:
        return str(obj.id)

    def label(self, obj: Any) -> str:
        return str(obj.filename)

    async def classification(self, db: AsyncSession, obj: Any) -> str:
        return DEFAULT_CLASSIFICATION

    async def summary(self, db: AsyncSession, obj: Any) -> str:
        return f"{obj.content_type}, {obj.size_bytes} bytes"

    def deep_link(self, obj: Any) -> str | None:
        return None

    async def read(self, db: AsyncSession, obj: Any, page: int) -> ContextPage | None:
        return paginate(obj.extracted_text or "", page)

    async def search(self, db: AsyncSession, viewer: Viewer, query: str, limit: int) -> list[Any]:
        """Files from the viewer's own chats, newest first."""
        if not self.admits(viewer):
            return []
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
