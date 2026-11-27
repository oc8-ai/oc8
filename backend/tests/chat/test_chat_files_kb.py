"""A chat's own knowledge base (spec §5.4): one hidden base per chat session that
holds its large files. It must never show up anywhere the organisation's
knowledge bases do -- lists, pickers, grants, context search, an agent's normal
retrieval -- and it goes when its chat goes."""

from __future__ import annotations

import hashlib
import uuid

import httpx
import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.chat.context.base import viewer_for_member
from oc8.chat.context.manifest import build_manifest
from oc8.chat.context.registry import RESOLVERS, resolve_ref
from oc8.chat.files_kb import chat_kb_for, drop_chat_kb
from oc8.chat.service import _resolve_context_refs, delete_session
from oc8.config import get_settings
from oc8.knowledge.connectors.base import RawDocument
from oc8.knowledge.ingest import ingest_raw_document
from oc8.knowledge.retrieval import retrieve_kb_context
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())


async def _chat(db: object, tenant: uuid.UUID) -> tuple[m.OrgMember, m.ChatSession]:
    member = m.OrgMember(tenant_id=tenant, subject=uuid.uuid4().hex, subject_uuid=uuid.uuid4())
    db.add(member)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    session = m.ChatSession(tenant_id=tenant, agent_id=uuid.uuid4(), member_id=member.id)
    db.add(session)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    return member, session


async def _put_file(db: object, tenant: uuid.UUID, kb: m.KnowledgeBase, text: str) -> None:
    source = await db.get(m.DataSource, uuid.UUID(kb.source_ids[0]))  # type: ignore[attr-defined]
    assert source is not None
    await ingest_raw_document(
        db,  # type: ignore[arg-type]
        tenant_id=tenant,
        data_source=source,
        kb_id=kb.id,
        raw=RawDocument(
            source_uri=f"chat://{kb.chat_session_id}/report.txt",
            title="report.txt",
            content=text,
            content_type="text/plain",
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
        ),
    )


async def test_chat_kb_is_created_lazily_and_reused(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        _member, session = await _chat(db, tenant)
        assert await chat_kb_for(db, session=session, create=False) is None

        kb = await chat_kb_for(db, session=session, create=True)
        assert kb is not None
        assert kb.kind == "chat"
        assert kb.chat_session_id == session.id
        assert kb.classification == "internal"
        assert kb.embedding_model == get_settings().default_embedding_model
        source = await db.get(m.DataSource, uuid.UUID(kb.source_ids[0]))
        assert source is not None and source.connector_type == "chat_upload"

        again = await chat_kb_for(db, session=session, create=True)
        assert again is not None and again.id == kb.id and again.source_ids == kb.source_ids
        found = await chat_kb_for(db, session=session, create=False)
        assert found is not None and found.id == kb.id
        sources = (
            await db.execute(
                select(m.DataSource).where(m.DataSource.connector_type == "chat_upload")
            )
        ).scalars()
        assert len(list(sources)) == 1


async def test_chat_kb_hidden_everywhere(app_session: AppSessionFactory) -> None:
    from asgi_lifespan import LifespanManager

    from oc8.auth import get_identity_provider
    from oc8.main import create_app

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        member, session = await _chat(db, tenant)
        shared = m.KnowledgeBase(tenant_id=tenant, name="Files handbook", embedding_model="x")
        db.add(shared)
        await db.flush()
        kb = await chat_kb_for(db, session=session, create=True)
        assert kb is not None
        kb.name = "Files of the chat"
        await _put_file(db, tenant, kb, "the secret launch date is friday")
        source_id = uuid.UUID(kb.source_ids[0])
        dept = m.Department(tenant_id=tenant, name="Sales")
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora")
        db.add(agent)
        await db.flush()
        # Even a grant written straight into the table (no route creates one)
        # does not make the base part of an agent's normal search.
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        await db.flush()
        kb_id, agent_id, member_id, session_id = kb.id, agent.id, member.id, session.id

    # The knowledge API: not listed, not fetchable, not grantable, its source
    # not listed either.
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    headers = {"Authorization": f"Bearer {token}"}
    app = create_app()
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            for archived in ("false", "true"):
                r = await c.get(
                    f"/api/v1/knowledge/bases?includeArchived={archived}&limit=200",
                    headers=headers,
                )
                assert r.status_code == 200, r.text
                names = [b["name"] for b in r.json()["items"]]
                assert names == ["Files handbook"]
            r = await c.get(f"/api/v1/knowledge/bases/{kb_id}", headers=headers)
            assert r.status_code == 404
            r = await c.get(f"/api/v1/knowledge/bases/{kb_id}/documents", headers=headers)
            assert r.status_code == 404
            r = await c.post(
                "/api/v1/knowledge/grants",
                json={"kbId": str(kb_id), "granteeType": "agent", "granteeId": str(agent_id)},
                headers=headers,
            )
            assert 400 <= r.status_code < 500, r.text
            r = await c.post(
                f"/api/v1/knowledge/bases/{kb_id}/documents",
                json={"filename": "a.txt", "content": "x", "contentType": "text/plain"},
                headers=headers,
            )
            assert r.status_code == 404
            r = await c.delete(f"/api/v1/knowledge/bases/{kb_id}", headers=headers)
            assert r.status_code == 404
            r = await c.get("/api/v1/knowledge/sources?includeArchived=true", headers=headers)
            assert r.status_code == 200, r.text
            assert str(source_id) not in {s["id"] for s in r.json()["items"]}
            r = await c.delete(f"/api/v1/knowledge/sources/{source_id}", headers=headers)
            assert r.status_code == 404

    async with app_session(tenant) as db:
        member = await db.get(m.OrgMember, member_id)
        session = await db.get(m.ChatSession, session_id)
        agent = await db.get(m.Agent, agent_id)
        assert member is not None and session is not None and agent is not None
        viewer = await viewer_for_member(db, member, token_role="org_admin")

        # Context search and pinning (`#` picker, chat/project pins).
        found = await RESOLVERS["knowledge_base"].search(db, viewer, "Files", 50)
        assert [b.name for b in found] == ["Files handbook"]
        assert (
            await resolve_ref(db, viewer=viewer, kind="knowledge_base", ref_id=str(kb_id)) is None
        )
        assert (
            await resolve_ref(db, viewer=viewer, kind="data_source", ref_id=str(source_id)) is None
        )
        sources = await RESOLVERS["data_source"].search(db, viewer, "", 50)
        assert source_id not in {s.id for s in sources}
        manifest = await build_manifest(
            db,
            viewer=viewer,
            message_refs=[
                {"kind": "knowledge_base", "id": str(kb_id)},
                {"kind": "data_source", "id": str(source_id)},
            ],
            session=session,
        )
        assert manifest.entries == [] and manifest.released_kb_ids == []

        # A `#` reference on a turn without a manifest.
        refs, _restricted = await _resolve_context_refs(
            db,
            tenant_id=tenant,
            refs=[{"kind": "knowledge_base", "id": str(kb_id)}],
            acting_member=member,
            token_role="org_admin",
        )
        assert refs == []

        # An agent's normal retrieval -- with and without the chat's pins.
        for released in (None, frozenset({kb_id})):
            context, _ = await retrieve_kb_context(
                db,
                agent=agent,
                tenant_id=tenant,
                query_text="launch date",
                released_kb_ids=released,
            )
            assert "launch date" not in context

        # The one way in: the caller names the chat's base explicitly
        # (`search_chat_files`, Task 9).
        context, _ = await retrieve_kb_context(
            db,
            agent=agent,
            tenant_id=tenant,
            query_text="launch date",
            only_kb_ids=frozenset({kb_id}),
            released_kb_ids=frozenset({kb_id}),
        )
        assert "the secret launch date is friday" in context


async def test_session_delete_drops_chat_kb(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        _member, session = await _chat(db, tenant)
        _other_member, other = await _chat(db, tenant)
        kb = await chat_kb_for(db, session=session, create=True)
        kept = await chat_kb_for(db, session=other, create=True)
        assert kb is not None and kept is not None
        await _put_file(db, tenant, kb, "quarterly numbers are up")
        await _put_file(db, tenant, kept, "another chat's file")
        kb_id, kept_id, session_id = kb.id, kept.id, session.id
        source_id = uuid.UUID(kb.source_ids[0])
        await delete_session(db, tenant_id=tenant, session=session)

    async with app_session(tenant) as db:
        base = await db.get(m.KnowledgeBase, kb_id)
        assert base is not None and base.deleted_at is not None
        source = await db.get(m.DataSource, source_id)
        assert source is not None and source.deleted_at is not None
        chunks = list(
            (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb_id))).scalars()
        )
        assert chunks and all(
            c.deleted_at is not None and c.content == "" and c.embedding is None for c in chunks
        )
        assert await db.get(m.ChatSession, session_id) is None
        survivor = await db.get(m.KnowledgeBase, kept_id)
        assert survivor is not None and survivor.deleted_at is None
        live = list(
            (
                await db.execute(
                    select(m.KbChunk).where(
                        m.KbChunk.kb_id == kept_id, m.KbChunk.deleted_at.is_(None)
                    )
                )
            ).scalars()
        )
        assert live


async def test_drop_chat_kb_without_a_base_is_a_no_op_and_a_new_base_can_follow(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        _member, session = await _chat(db, tenant)
        await drop_chat_kb(db, session=session)
        first = await chat_kb_for(db, session=session, create=True)
        assert first is not None
        await drop_chat_kb(db, session=session)
        assert await chat_kb_for(db, session=session, create=False) is None
        second = await chat_kb_for(db, session=session, create=True)
        assert second is not None and second.id != first.id


async def test_a_failed_ingest_locks_the_file_row_before_the_job(
    app_session: AppSessionFactory,
) -> None:
    """The worker's fail paths change the job before they settle the file:
    the job row must still be written after the file's row lock, the order
    `forget_chat_file` takes them in -- the other way round, the two wait on
    each other."""
    from oc8.chat.files_kb import CHAT_FILE_JOB, finish_chat_file
    from tests.chat.test_message_tree import statements

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        _member, session = await _chat(db, tenant)
        kb = await chat_kb_for(db, session=session, create=True)
        assert kb is not None
        attachment = m.FileAttachment(
            tenant_id=tenant,
            owner_type="chat_message",
            owner_id=session.id,
            bucket_key=f"k/{uuid.uuid4()}",
            filename="big.txt",
            content_type="text/plain",
            size_bytes=10,
            index_status="queued",
        )
        db.add(attachment)
        await db.flush()
        job = m.IngestionJob(
            tenant_id=tenant,
            data_source_id=uuid.UUID(kb.source_ids[0]),
            kb_id=kb.id,
            status="running",
            stats={"kind": CHAT_FILE_JOB, "attachment_id": str(attachment.id)},
        )
        db.add(job)
        await db.flush()
        job.status = "failed"
        with statements() as seen:
            assert await finish_chat_file(db, attachment_id=attachment.id, status="failed")
            await db.flush()
        await db.refresh(attachment, ["index_status"])
        assert attachment.index_status == "failed"
    file_lock = next(
        i for i, sql in enumerate(seen) if "FROM file_attachment" in sql and "FOR UPDATE" in sql
    )
    job_write = next(i for i, sql in enumerate(seen) if "UPDATE ingestion_job" in sql)
    assert file_lock < job_write, seen
