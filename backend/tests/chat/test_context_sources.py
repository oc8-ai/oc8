"""Chat context refs for knowledge, sources and files (oc8 Chat spec §5.1).

Every ref is resolved under the VIEWING member's rights, reusing the rule the
rest of oc8 applies to that object: `knowledge:view` for bases and sources
(what `require_permission` gates `/knowledge/*` on), session ownership or
audience for a chat file, agent visibility for a run's file (`api/v1/files.py`).
Whatever the member may not see is dropped -- unknown, other-tenant, deleted
and unauthorised read identically, so a ref never confirms that a row exists."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.permissions import KNOWLEDGE, VIEW, perm
from oc8.chat.context.base import PAGE_CHARS, Viewer, paginate, viewer_for_member
from oc8.chat.context.registry import RESOLVERS, resolve_ref, resolve_refs
from oc8.knowledge import browse
from oc8.knowledge.connectors.base import ConnectorError, FetchedFile
from tests.conftest import AppSessionFactory


async def _people(db: AsyncSession, tenant: uuid.UUID) -> tuple[Viewer, Viewer]:
    admin = m.OrgMember(
        tenant_id=tenant, subject="admin", subject_uuid=uuid.uuid4(), display_name="Admin"
    )
    emp = m.OrgMember(
        tenant_id=tenant, subject="emp", subject_uuid=uuid.uuid4(), display_name="Emp"
    )
    db.add_all([admin, emp])
    await db.flush()
    return (
        await viewer_for_member(db, admin, token_role="org_admin"),
        await viewer_for_member(db, emp, token_role="member"),
    )


async def _member(db: AsyncSession, tenant: uuid.UUID, subject: str) -> m.OrgMember:
    row = m.OrgMember(tenant_id=tenant, subject=subject, subject_uuid=uuid.uuid4())
    db.add(row)
    await db.flush()
    return row


def _kb_ref(kb: m.KnowledgeBase) -> dict[str, str]:
    return {"kind": "knowledge_base", "id": str(kb.id)}


# ------------------------------------------------------------------ knowledge base


async def test_knowledge_base_needs_the_members_own_access(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        admin, emp = await _people(db, tenant)
        kb = m.KnowledgeBase(
            tenant_id=tenant, name="Prices", embedding_model="x", classification="internal"
        )
        gone = m.KnowledgeBase(tenant_id=tenant, name="Old", embedding_model="x")
        db.add_all([kb, gone])
        await db.flush()
        gone.deleted_at = datetime.now(tz=UTC)
        await db.flush()
        refs = [
            _kb_ref(kb),
            _kb_ref(gone),
            {"kind": "nonsense", "id": "x"},
            {"kind": "knowledge_base", "id": str(uuid.uuid4())},
            {"kind": "knowledge_base", "id": "not-a-uuid"},
            {"id": str(kb.id)},
        ]
        as_admin = await resolve_refs(db, viewer=admin, refs=refs)
        as_emp = await resolve_refs(db, viewer=emp, refs=refs)
    assert [r.label for r in as_admin] == ["Prices"]
    assert as_admin[0].kind == "knowledge_base" and as_admin[0].id == str(kb.id)
    assert as_admin[0].readable is False
    assert as_admin[0].deep_link == f"/knowledge/bases/{kb.id}/content"
    assert as_emp == []


async def test_order_is_kept_and_duplicates_collapse(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        admin, _emp = await _people(db, tenant)
        first = m.KnowledgeBase(tenant_id=tenant, name="B first", embedding_model="x")
        second = m.KnowledgeBase(tenant_id=tenant, name="A second", embedding_model="x")
        db.add_all([first, second])
        await db.flush()
        refs = [_kb_ref(first), {"kind": "nonsense", "id": "1"}, _kb_ref(second), _kb_ref(first)]
        resolved = await resolve_refs(db, viewer=admin, refs=refs)
    assert [r.label for r in resolved] == ["B first", "A second"]


async def test_a_tenant_defined_role_holding_knowledge_view_is_admitted(
    app_session: AppSessionFactory,
) -> None:
    """The same answer `require_permission(knowledge:view)` gives the KB screens."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        role = m.Role(tenant_id=tenant, name="kb-readers", kind="human")
        db.add(role)
        await db.flush()
        db.add(
            m.RolePermission(tenant_id=tenant, role_id=role.id, permission=perm(KNOWLEDGE, VIEW))
        )
        reader = m.OrgMember(
            tenant_id=tenant, subject="reader", subject_uuid=uuid.uuid4(), role_id=role.id
        )
        kb = m.KnowledgeBase(tenant_id=tenant, name="Handbook", embedding_model="x")
        db.add_all([reader, kb])
        await db.flush()
        viewer = await viewer_for_member(db, reader, token_role="member")
        resolved = await resolve_refs(db, viewer=viewer, refs=[_kb_ref(kb)])
    assert [r.label for r in resolved] == ["Handbook"]


async def test_knowledge_base_classification(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        admin, _emp = await _people(db, tenant)
        plain = m.KnowledgeBase(tenant_id=tenant, name="Plain", embedding_model="x")
        secret = m.KnowledgeBase(
            tenant_id=tenant, name="Secret", embedding_model="x", classification="confidential"
        )
        local = m.KnowledgeBase(
            tenant_id=tenant, name="Local", embedding_model="x", local_only=True
        )
        db.add_all([plain, secret, local])
        await db.flush()
        resolved = await resolve_refs(
            db, viewer=admin, refs=[_kb_ref(plain), _kb_ref(secret), _kb_ref(local)]
        )
    assert [r.classification for r in resolved] == ["internal", "confidential", "restricted"]


async def test_another_tenants_objects_are_dropped(app_session: AppSessionFactory) -> None:
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    async with app_session(theirs) as db:
        kb = m.KnowledgeBase(tenant_id=theirs, name="Theirs", embedding_model="x")
        src = m.DataSource(tenant_id=theirs, connector_type="upload", name="Their drive")
        db.add_all([kb, src])
        await db.flush()
        owner = await _member(db, theirs, "them")
        session = m.ChatSession(tenant_id=theirs, agent_id=uuid.uuid4(), member_id=owner.id)
        db.add(session)
        await db.flush()
        att = m.FileAttachment(
            tenant_id=theirs,
            owner_type="chat_message",
            owner_id=session.id,
            bucket_key=f"k/{uuid.uuid4()}",
            filename="theirs.txt",
            content_type="text/plain",
            size_bytes=3,
            extracted_text="abc",
        )
        db.add(att)
        await db.flush()
    refs = [
        _kb_ref(kb),
        {"kind": "data_source", "id": str(src.id)},
        {"kind": "source_file", "id": f"{src.id}:a.txt"},
        {"kind": "file", "id": str(att.id)},
    ]
    async with app_session(mine) as db:
        admin, _emp = await _people(db, mine)
        assert await resolve_refs(db, viewer=admin, refs=refs) == []


async def test_an_offboarded_viewer_resolves_nothing(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        admin, _emp = await _people(db, tenant)
        kb = m.KnowledgeBase(tenant_id=tenant, name="Prices", embedding_model="x")
        db.add(kb)
        await db.flush()
        admin.member.deleted_at = datetime.now(tz=UTC)
        await db.flush()
        assert await resolve_refs(db, viewer=admin, refs=[_kb_ref(kb)]) == []


# --------------------------------------------------------------------- data source


async def test_data_source_follows_knowledge_view(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        admin, emp = await _people(db, tenant)
        src = m.DataSource(
            tenant_id=tenant, connector_type="upload", name="Drive", classification="confidential"
        )
        gone = m.DataSource(tenant_id=tenant, connector_type="upload", name="Old drive")
        db.add_all([src, gone])
        await db.flush()
        gone.deleted_at = datetime.now(tz=UTC)
        await db.flush()
        refs = [
            {"kind": "data_source", "id": str(src.id)},
            {"kind": "data_source", "id": str(gone.id)},
            {"kind": "data_source", "id": str(uuid.uuid4())},
        ]
        as_admin = await resolve_refs(db, viewer=admin, refs=refs)
        as_emp = await resolve_refs(db, viewer=emp, refs=refs)
    assert [(r.label, r.classification, r.readable) for r in as_admin] == [
        ("Drive", "confidential", False)
    ]
    assert as_emp == []


# --------------------------------------------------------------------- source file


async def test_source_file_is_gated_by_its_source_and_read_in_pages(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[uuid.UUID, str]] = []

    async def fake_fetch(
        db: AsyncSession, *, source: m.DataSource, file_id: str, known_size: int | None = None
    ) -> FetchedFile:
        calls.append((source.id, file_id))
        return FetchedFile(
            name="q3.txt", content_type="text/plain", data=b"", text="y" * (PAGE_CHARS + 5)
        )

    monkeypatch.setattr(browse, "fetch_source_file", fake_fetch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        admin, emp = await _people(db, tenant)
        src = m.DataSource(
            tenant_id=tenant, connector_type="upload", name="Drive", classification="restricted"
        )
        gone = m.DataSource(tenant_id=tenant, connector_type="upload", name="Old")
        db.add_all([src, gone])
        await db.flush()
        gone.deleted_at = datetime.now(tz=UTC)
        await db.flush()
        ref_id = f"{src.id}:reports/2026/q3.txt"
        resolved = await resolve_ref(db, viewer=admin, kind="source_file", ref_id=ref_id)
        assert resolved is not None
        ref, obj = resolved
        page = await RESOLVERS["source_file"].read(db, obj, 2)
        assert await resolve_ref(db, viewer=emp, kind="source_file", ref_id=ref_id) is None
        dropped = await resolve_refs(
            db,
            viewer=admin,
            refs=[
                {"kind": "source_file", "id": str(src.id)},
                {"kind": "source_file", "id": f"{src.id}:"},
                {"kind": "source_file", "id": f"{gone.id}:a.txt"},
                {"kind": "source_file", "id": f"{uuid.uuid4()}:a.txt"},
                {"kind": "source_file", "id": "junk:a.txt"},
            ],
        )
    assert (ref.label, ref.classification, ref.readable, ref.id) == (
        "q3.txt",
        "restricted",
        True,
        ref_id,
    )
    assert calls == [(src.id, "reports/2026/q3.txt")]
    assert page is not None and (page.page, page.pages, page.text) == (2, 2, "yyyyy")
    assert dropped == []


async def test_source_file_on_a_connector_that_cannot_fetch_reads_nothing(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unsupported(db: AsyncSession, **_: Any) -> FetchedFile:
        raise browse.BrowseUnsupported("upload")

    monkeypatch.setattr(browse, "fetch_source_file", unsupported)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        admin, _emp = await _people(db, tenant)
        src = m.DataSource(tenant_id=tenant, connector_type="upload", name="Drive")
        db.add(src)
        await db.flush()
        resolved = await resolve_ref(db, viewer=admin, kind="source_file", ref_id=f"{src.id}:a")
        assert resolved is not None
        assert await RESOLVERS["source_file"].read(db, resolved[1], 1) is None


async def test_a_connector_failure_on_read_is_raised_not_hidden(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Too large, unreachable: the caller (the read tool) reports why."""

    async def too_large(db: AsyncSession, **_: Any) -> FetchedFile:
        raise ConnectorError("file is too large to pin")

    monkeypatch.setattr(browse, "fetch_source_file", too_large)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        admin, _emp = await _people(db, tenant)
        src = m.DataSource(tenant_id=tenant, connector_type="upload", name="Drive")
        db.add(src)
        await db.flush()
        resolved = await resolve_ref(db, viewer=admin, kind="source_file", ref_id=f"{src.id}:a")
        assert resolved is not None
        with pytest.raises(ConnectorError):
            await RESOLVERS["source_file"].read(db, resolved[1], 1)


# ---------------------------------------------------------------------------- file


async def _chat_file(
    db: AsyncSession, tenant: uuid.UUID, owner: m.OrgMember, *, sent: bool, text: str
) -> m.FileAttachment:
    session = m.ChatSession(tenant_id=tenant, agent_id=uuid.uuid4(), member_id=owner.id)
    db.add(session)
    await db.flush()
    owner_id = session.id
    if sent:
        msg = m.ChatMessage(tenant_id=tenant, session_id=session.id, role="user", content="see")
        db.add(msg)
        await db.flush()
        owner_id = msg.id
    att = m.FileAttachment(
        tenant_id=tenant,
        owner_type="chat_message",
        owner_id=owner_id,
        bucket_key=f"k/{uuid.uuid4()}",
        filename="notes.txt",
        content_type="text/plain",
        size_bytes=len(text),
        extracted_text=text,
    )
    db.add(att)
    await db.flush()
    return att


async def test_a_file_from_my_own_chat_is_readable_in_pages(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        admin, emp = await _people(db, tenant)
        att = await _chat_file(db, tenant, emp.member, sent=True, text="x" * 20000)
        resolved = await resolve_ref(db, viewer=emp, kind="file", ref_id=str(att.id))
        assert resolved is not None
        ref, obj = resolved
        page = await RESOLVERS["file"].read(db, obj, 2)
        stranger = await viewer_for_member(db, await _member(db, tenant, "o"), token_role="member")
        assert await resolve_ref(db, viewer=stranger, kind="file", ref_id=str(att.id)) is None
        # Not even an org admin reads another member's chat file: the download
        # route refuses them too.
        assert await resolve_ref(db, viewer=admin, kind="file", ref_id=str(att.id)) is None
    assert ref.readable and ref.label == "notes.txt"
    assert (ref.size, ref.classification, ref.deep_link) == (20000, "internal", None)
    assert page is not None and page.page == 2 and page.pages == 3


async def test_a_file_uploaded_before_its_message_was_sent_resolves(
    app_session: AppSessionFactory,
) -> None:
    """`owner_id` is the session id until the message is sent (files.py)."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        _admin, emp = await _people(db, tenant)
        att = await _chat_file(db, tenant, emp.member, sent=False, text="draft")
        resolved = await resolve_ref(db, viewer=emp, kind="file", ref_id=str(att.id))
        missing = await resolve_ref(db, viewer=emp, kind="file", ref_id=str(uuid.uuid4()))
    assert resolved is not None and resolved[0].label == "notes.txt"
    assert missing is None


async def test_a_run_output_file_follows_agent_visibility(
    app_session: AppSessionFactory,
) -> None:
    """The rule `GET /files/{id}` applies to an `agent_run` file."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        admin, emp = await _people(db, tenant)
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Reporter")
        db.add(agent)
        await db.flush()
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, source="manual", context={})
        db.add(run)
        await db.flush()
        att = m.FileAttachment(
            tenant_id=tenant,
            owner_type="agent_run",
            owner_id=run.id,
            bucket_key=f"k/{uuid.uuid4()}",
            filename="report.md",
            content_type="text/markdown",
            size_bytes=6,
            extracted_text="# Done",
        )
        db.add(att)
        await db.flush()
        ref_id = str(att.id)
        as_admin = await resolve_ref(db, viewer=admin, kind="file", ref_id=ref_id)
        as_emp_without_seat = await resolve_ref(db, viewer=emp, kind="file", ref_id=ref_id)
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=emp.member.id,
                department_id=dept.id,
                seat_role="dept_viewer",
            )
        )
        await db.flush()
        seated = await viewer_for_member(db, emp.member, token_role="member")
        as_emp_with_seat = await resolve_ref(db, viewer=seated, kind="file", ref_id=ref_id)
    assert as_admin is not None and as_admin[0].label == "report.md"
    assert as_emp_without_seat is None
    assert as_emp_with_seat is not None


# ------------------------------------------------------------------------ paging


def test_paginate_clamps() -> None:
    page = paginate("abc", 9)
    assert (page.page, page.pages, page.text) == (1, 1, "abc")
    assert (paginate("", 0).page, paginate("", 0).pages) == (1, 1)
    two = paginate("z" * (PAGE_CHARS + 1), -3)
    assert (two.page, two.pages, len(two.text)) == (1, 2, PAGE_CHARS)
