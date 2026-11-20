"""Chat context refs for knowledge, sources and files (oc8 Chat spec §5.1).

Every ref is resolved under the VIEWING member's rights, reusing the rule the
rest of oc8 applies to that object: `knowledge:view` for bases and sources
(what `require_permission` gates `/knowledge/*` on), and for a file exactly
what `GET /files/{id}` would serve (`storage/attachment_access.py`).
Whatever the member may not see is dropped -- unknown, other-tenant, deleted
and unauthorised read identically, so a ref never confirms that a row exists."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.permissions import KNOWLEDGE, VIEW, perm
from oc8.chat.context.base import PAGE_CHARS, Viewer, paginate, viewer_for_member
from oc8.chat.context.registry import RESOLVERS, resolve_ref, resolve_refs
from oc8.chat.context.sources import KnowledgeBaseResolver
from oc8.knowledge.connectors.base import BrowseEntry, ConnectorError, FetchedFile
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


async def test_a_source_inside_a_local_only_base_is_restricted(
    app_session: AppSessionFactory,
) -> None:
    """The label its content carries once ingested (`knowledge/ingest.py`)."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        admin, _emp = await _people(db, tenant)
        local_src = m.DataSource(tenant_id=tenant, connector_type="upload", name="Local")
        conf_src = m.DataSource(tenant_id=tenant, connector_type="upload", name="Conf")
        free_src = m.DataSource(tenant_id=tenant, connector_type="upload", name="Free")
        db.add_all([local_src, conf_src, free_src])
        await db.flush()
        gone = m.KnowledgeBase(
            tenant_id=tenant,
            name="Gone",
            embedding_model="x",
            classification="restricted",
            source_ids=[str(free_src.id)],
            deleted_at=datetime.now(tz=UTC),
        )
        db.add_all(
            [
                m.KnowledgeBase(
                    tenant_id=tenant,
                    name="Local base",
                    embedding_model="x",
                    local_only=True,
                    source_ids=[str(local_src.id)],
                ),
                m.KnowledgeBase(
                    tenant_id=tenant,
                    name="Conf base",
                    embedding_model="x",
                    classification="confidential",
                    source_ids=[str(conf_src.id), str(free_src.id) + "x"],
                ),
                gone,
            ]
        )
        await db.flush()
        resolved = await resolve_refs(
            db,
            viewer=admin,
            refs=[
                {"kind": "data_source", "id": str(s.id)} for s in (local_src, conf_src, free_src)
            ],
        )
    assert [(r.label, r.classification) for r in resolved] == [
        ("Local", "restricted"),
        ("Conf", "confidential"),
        ("Free", "internal"),
    ]


async def test_knowledge_kinds_are_not_loaded_for_a_viewer_without_knowledge_view(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No load, so no existence-timing oracle."""
    loads: list[str] = []
    original = KnowledgeBaseResolver.load

    async def counting(self: Any, db: AsyncSession, tenant_id: Any, ref_id: str) -> Any:
        loads.append(ref_id)
        return await original(self, db, tenant_id, ref_id)

    monkeypatch.setattr(KnowledgeBaseResolver, "load", counting)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        admin, emp = await _people(db, tenant)
        kb = m.KnowledgeBase(tenant_id=tenant, name="Prices", embedding_model="x")
        db.add(kb)
        await db.flush()
        assert await resolve_refs(db, viewer=emp, refs=[_kb_ref(kb)]) == []
        assert loads == []
        assert len(await resolve_refs(db, viewer=admin, refs=[_kb_ref(kb)])) == 1
        assert loads == [str(kb.id)]


async def test_ids_are_canonical_and_duplicates_collapse_on_them(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        admin, _emp = await _people(db, tenant)
        kb = m.KnowledgeBase(tenant_id=tenant, name="Prices", embedding_model="x")
        db.add(kb)
        await db.flush()
        shouted = {"kind": "knowledge_base", "id": str(kb.id).upper()}
        resolved = await resolve_refs(db, viewer=admin, refs=[shouted, _kb_ref(kb)])
    assert [r.id for r in resolved] == [str(kb.id)]


# --------------------------------------------------------------------- source file


class Drive:
    """A browsing connector whose source exposes `reports/` and nothing else."""

    type_id = "fake_drive"

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.fetched: list[str] = []

    async def browse(self, config: dict[str, Any], path: str, auth: Any) -> list[BrowseEntry]:
        return []

    async def in_scope(self, config: dict[str, Any], file_id: str, auth: Any) -> bool:
        return file_id.startswith("reports/")

    async def fetch_file(
        self, config: dict[str, Any], file_id: str, max_bytes: int, auth: Any
    ) -> FetchedFile:
        self.fetched.append(file_id)
        return FetchedFile(name=file_id, content_type="text/plain", data=b"", text=self.text)


class CannotFetch:
    type_id = "fake_site"


class TooLarge(Drive):
    async def fetch_file(
        self, config: dict[str, Any], file_id: str, max_bytes: int, auth: Any
    ) -> FetchedFile:
        raise RuntimeError("413 for https://u:hunter2@host")


async def _drive(db: AsyncSession, tenant: uuid.UUID, **kw: Any) -> m.DataSource:
    src = m.DataSource(tenant_id=tenant, connector_type="fake_drive", name="Drive", **kw)
    db.add(src)
    await db.flush()
    return src


async def test_source_file_is_gated_by_its_source_and_read_in_pages(
    app_session: AppSessionFactory,
) -> None:
    drive = Drive(text="y" * (PAGE_CHARS + 5))
    tenant = uuid.uuid4()
    with patch("oc8.knowledge.browse.resolve_connector", return_value=drive):
        async with app_session(tenant) as db:
            admin, emp = await _people(db, tenant)
            src = await _drive(db, tenant, classification="restricted")
            gone = await _drive(db, tenant, deleted_at=datetime.now(tz=UTC))
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
                    {"kind": "source_file", "id": f"{src.id}:reports/a\nb"},
                    {"kind": "source_file", "id": f"{src.id}:" + "r" * 5000},
                    {"kind": "source_file", "id": f"{gone.id}:reports/a.txt"},
                    {"kind": "source_file", "id": f"{uuid.uuid4()}:reports/a.txt"},
                    {"kind": "source_file", "id": "junk:reports/a.txt"},
                ],
            )
    assert (ref.label, ref.classification, ref.readable, ref.id) == (
        "q3.txt",
        "restricted",
        True,
        ref_id,
    )
    assert drive.fetched == ["reports/2026/q3.txt"]
    assert page is not None and (page.page, page.pages, page.text) == (2, 2, "yyyyy")
    assert dropped == []


async def test_a_file_outside_the_sources_scope_is_dropped(
    app_session: AppSessionFactory,
) -> None:
    """Ruling 29: the connection may reach it, the source does not expose it."""
    drive = Drive(text="secret")
    tenant = uuid.uuid4()
    with patch("oc8.knowledge.browse.resolve_connector", return_value=drive):
        async with app_session(tenant) as db:
            admin, _emp = await _people(db, tenant)
            src = await _drive(db, tenant)
            outside = await resolve_ref(
                db, viewer=admin, kind="source_file", ref_id=f"{src.id}:hr/salaries.xlsx"
            )
    assert outside is None
    assert drive.fetched == []


async def test_a_source_that_cannot_fetch_single_files_pins_none(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    with patch("oc8.knowledge.browse.resolve_connector", return_value=CannotFetch()):
        async with app_session(tenant) as db:
            admin, _emp = await _people(db, tenant)
            src = await _drive(db, tenant)
            assert (
                await resolve_ref(
                    db, viewer=admin, kind="source_file", ref_id=f"{src.id}:reports/a"
                )
                is None
            )


async def test_reading_a_file_the_connector_cannot_find_reads_nothing(
    app_session: AppSessionFactory,
) -> None:
    """Pinned while the connector browsed; the connector since lost browse."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        admin, _emp = await _people(db, tenant)
        src = await _drive(db, tenant)
        with patch("oc8.knowledge.browse.resolve_connector", return_value=Drive()):
            resolved = await resolve_ref(
                db, viewer=admin, kind="source_file", ref_id=f"{src.id}:reports/a"
            )
        assert resolved is not None
        with patch("oc8.knowledge.browse.resolve_connector", return_value=CannotFetch()):
            assert await RESOLVERS["source_file"].read(db, resolved[1], 1) is None


async def test_a_connector_failure_on_read_is_raised_not_hidden(
    app_session: AppSessionFactory,
) -> None:
    """The read tool reports why; the message never carries connector text."""
    tenant = uuid.uuid4()
    with patch("oc8.knowledge.browse.resolve_connector", return_value=TooLarge()):
        async with app_session(tenant) as db:
            admin, _emp = await _people(db, tenant)
            src = await _drive(db, tenant)
            resolved = await resolve_ref(
                db, viewer=admin, kind="source_file", ref_id=f"{src.id}:reports/a"
            )
            assert resolved is not None
            with pytest.raises(ConnectorError) as ei:
                await RESOLVERS["source_file"].read(db, resolved[1], 1)
    assert "hunter2" not in str(ei.value)


# ---------------------------------------------------------------------------- file


async def _seated(db: AsyncSession, tenant: uuid.UUID, member: m.OrgMember) -> Viewer:
    """A plain member with an `agent:view` seat -- what `/files/{id}` admits."""
    dept = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex[:6]}", frame={})
    db.add(dept)
    await db.flush()
    db.add(
        m.OrgMemberDepartment(
            tenant_id=tenant, member_id=member.id, department_id=dept.id, seat_role="dept_viewer"
        )
    )
    await db.flush()
    return await viewer_for_member(db, member, token_role="member")


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
        me = await _seated(db, tenant, emp.member)
        att = await _chat_file(db, tenant, emp.member, sent=True, text="x" * 20000)
        resolved = await resolve_ref(db, viewer=me, kind="file", ref_id=str(att.id))
        assert resolved is not None
        ref, obj = resolved
        page = await RESOLVERS["file"].read(db, obj, 2)
        stranger = await _seated(db, tenant, await _member(db, tenant, "o"))
        assert await resolve_ref(db, viewer=stranger, kind="file", ref_id=str(att.id)) is None
        # Not even an org admin reads another member's chat file: the download
        # route refuses them too.
        assert await resolve_ref(db, viewer=admin, kind="file", ref_id=str(att.id)) is None
    assert ref.readable and ref.label == "notes.txt" and ref.id == str(att.id)
    assert (ref.size, ref.classification, ref.deep_link) == (20000, "internal", None)
    assert page is not None and page.page == 2 and page.pages == 3


async def test_a_member_the_files_route_refuses_cannot_pin_even_their_own_file(
    app_session: AppSessionFactory,
) -> None:
    """Without `agent:view` anywhere, `/files/{id}` 403s -- so pinning refuses."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        _admin, emp = await _people(db, tenant)
        att = await _chat_file(db, tenant, emp.member, sent=True, text="x")
        assert await resolve_ref(db, viewer=emp, kind="file", ref_id=str(att.id)) is None
        assert await RESOLVERS["file"].search(db, emp, "notes", 5) == []


async def test_a_file_uploaded_before_its_message_was_sent_resolves(
    app_session: AppSessionFactory,
) -> None:
    """`owner_id` is the session id until the message is sent (files.py)."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        _admin, emp = await _people(db, tenant)
        me = await _seated(db, tenant, emp.member)
        att = await _chat_file(db, tenant, emp.member, sent=False, text="draft")
        resolved = await resolve_ref(db, viewer=me, kind="file", ref_id=str(att.id))
        missing = await resolve_ref(db, viewer=me, kind="file", ref_id=str(uuid.uuid4()))
        found = await RESOLVERS["file"].search(db, me, "notes", 5)
    assert resolved is not None and resolved[0].label == "notes.txt"
    assert missing is None
    assert [f.id for f in found] == [att.id]


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
        elsewhere = await _seated(db, tenant, emp.member)
        as_emp_seated_elsewhere = await resolve_ref(
            db, viewer=elsewhere, kind="file", ref_id=ref_id
        )
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
        # A run output is a run read: the seat reaches the agent, and run:view
        # (an operator's, every agent's runs) reaches its runs.
        as_operator = await resolve_ref(
            db,
            viewer=await viewer_for_member(db, emp.member, token_role="operator"),
            kind="file",
            ref_id=ref_id,
        )
    assert as_admin is not None and as_admin[0].label == "report.md"
    assert as_emp_seated_elsewhere is None
    assert as_emp_with_seat is None
    assert as_operator is not None


# ------------------------------------------------------------------------ paging


def test_paginate_clamps() -> None:
    page = paginate("abc", 9)
    assert (page.page, page.pages, page.text) == (1, 1, "abc")
    assert (paginate("", 0).page, paginate("", 0).pages) == (1, 1)
    two = paginate("z" * (PAGE_CHARS + 1), -3)
    assert (two.page, two.pages, len(two.text)) == (1, 2, PAGE_CHARS)


def test_paginate_an_exact_multiple_has_no_empty_last_page() -> None:
    text = "a" * PAGE_CHARS + "b" * PAGE_CHARS
    last = paginate(text, 5)
    assert (last.page, last.pages, last.text) == (2, 2, "b" * PAGE_CHARS)


def test_paginate_counts_characters_not_bytes() -> None:
    text = "ü€😀" * PAGE_CHARS  # 3 * PAGE_CHARS characters, far more bytes
    pages = [paginate(text, n) for n in (1, 2, 3)]
    assert [p.pages for p in pages] == [3, 3, 3]
    assert "".join(p.text for p in pages) == text
    assert all(len(p.text) == PAGE_CHARS for p in pages)
