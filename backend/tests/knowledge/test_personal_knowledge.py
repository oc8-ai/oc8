"""A personal agent searches the knowledge of its owner's departments (spec A5).

Ruling R3: the owner holds a live seat in a live standard department D, and
the owner's resolved authority holds `knowledge:view`. Then the knowledge
bases granted to D join the personal agent's granted set -- on every call,
read only, nothing written. A hit from such a borrowed base must be cleared
by BOTH the personal frame and D's frame. Department memory written by D's
agents is never part of it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import update

from oc8 import models as m
from oc8.agent.control_tools import SEARCH_KNOWLEDGE, SEARCH_MEMORY, execute_control_tool
from oc8.agent.harness.caps import ModelCaps
from oc8.agent.preamble import build_run_preamble
from oc8.authz.pdp import Decision, Effect
from oc8.authz.permissions import AGENT, KNOWLEDGE, SEAT_VIEWER, VIEW, perm
from oc8.knowledge.ingest import ingest_document
from oc8.knowledge.retrieval import borrowed_kb_sources, granted_kb_ids, retrieve_kb_context
from oc8.memory.router import retrieve_context
from oc8.modelrouter import ToolCall
from oc8.models.core import DEPARTMENT_KIND_ASSISTANT, DEPARTMENT_KIND_PERSONAL
from oc8.models.knowledge import EMBED_DIM
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_KB_TEXT = "Der Rahmenvertrag mit Lieferant Nord gilt bis Ende 2027."
_RESTRICTED_TEXT = "Gehaltsband der Teamleitung liegt bei 95.000 Euro."
_DEPT_MEMORY = "Vertrieb merkt sich: Kunde Sued zahlt immer spaet."


class _FakeEmbedRouter:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(EMBED_DIM)]


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    for target in (
        "oc8.knowledge.ingest.get_model_router",
        "oc8.knowledge.retrieval.get_model_router",
        "oc8.memory.router.get_model_router",
    ):
        monkeypatch.setattr(target, lambda: _FakeEmbedRouter())


@dataclass(frozen=True)
class _World:
    tenant: uuid.UUID
    owner: m.OrgMember
    personal: m.Department
    dept: m.Department
    seat: m.OrgMemberDepartment
    kb: m.KnowledgeBase
    pia: m.Agent
    task: m.Task


async def _role(db: Any, tenant: uuid.UUID, *permissions: str) -> m.Role:
    role = m.Role(tenant_id=tenant, name=f"r-{uuid.uuid4().hex[:8]}", kind="human")
    db.add(role)
    await db.flush()
    for permission in permissions:
        db.add(m.RolePermission(tenant_id=tenant, role_id=role.id, permission=permission))
    await db.flush()
    return role


async def _member(db: Any, tenant: uuid.UUID, subject: str, role: m.Role) -> m.OrgMember:
    member = m.OrgMember(
        tenant_id=tenant,
        subject=subject,
        subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}:{tenant}"),
        display_name=subject.title(),
        role_id=role.id,
    )
    db.add(member)
    await db.flush()
    return member


async def _kb(db: Any, tenant: uuid.UUID, name: str, text: str) -> m.KnowledgeBase:
    kb = m.KnowledgeBase(tenant_id=tenant, name=name, embedding_model="nomic-embed-text")
    db.add(kb)
    await db.flush()
    await ingest_document(
        db,
        tenant_id=tenant,
        kb_id=kb.id,
        filename=f"{name}.txt",
        content=text,
        content_type="text/plain",
    )
    return kb


def _grant(tenant: uuid.UUID, kb: m.KnowledgeBase, grantee: m.Department | m.Agent) -> Any:
    return m.KnowledgeGrant(
        tenant_id=tenant,
        kb_id=kb.id,
        grantee_type="department" if isinstance(grantee, m.Department) else "agent",
        grantee_id=grantee.id,
    )


def _seat(tenant: uuid.UUID, member: m.OrgMember, dept: m.Department) -> m.OrgMemberDepartment:
    return m.OrgMemberDepartment(
        tenant_id=tenant, member_id=member.id, department_id=dept.id, seat_role=SEAT_VIEWER
    )


async def _world(
    db: Any,
    tenant: uuid.UUID,
    *,
    knowledge_view: bool = True,
    dept_frame: dict[str, Any] | None = None,
    personal_frame: dict[str, Any] | None = None,
) -> _World:
    granted = [perm(AGENT, VIEW)] + ([perm(KNOWLEDGE, VIEW)] if knowledge_view else [])
    owner = await _member(db, tenant, "owner", await _role(db, tenant, *granted))
    personal = m.Department(
        tenant_id=tenant,
        name="Owner",
        frame=personal_frame if personal_frame is not None else {"closed": True},
        kind=DEPARTMENT_KIND_PERSONAL,
        owner_member_id=owner.id,
    )
    dept = m.Department(
        tenant_id=tenant, name="Einkauf", frame=dept_frame if dept_frame is not None else {}
    )
    db.add_all([personal, dept])
    await db.flush()
    seat = _seat(tenant, owner, dept)
    db.add(seat)
    kb = await _kb(db, tenant, "Vertraege", _KB_TEXT)
    db.add(_grant(tenant, kb, dept))
    pia = m.Agent(
        tenant_id=tenant,
        department_id=personal.id,
        name="Pia",
        status="idle",
        definition={},
        presentation={},
        narrowing={},
    )
    db.add(pia)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=personal.id,
        assigned_agent_id=pia.id,
        title="Vertragslage",
        state="in_progress",
        delegation_depth=0,
    )
    db.add(task)
    await db.flush()
    return _World(
        tenant=tenant,
        owner=owner,
        personal=personal,
        dept=dept,
        seat=seat,
        kb=kb,
        pia=pia,
        task=task,
    )


async def _search(db: Any, w: _World, query: str = "Rahmenvertrag Lieferant") -> str:
    outcome = await execute_control_tool(
        db,
        tenant_id=w.tenant,
        agent=w.pia,
        task=w.task,
        tc=ToolCall(id=uuid.uuid4().hex, name=SEARCH_KNOWLEDGE.name, arguments={"query": query}),
        decision=Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
    )
    assert outcome is not None
    return outcome.output


async def _has_knowledge(db: Any, w: _World) -> bool:
    pre = await build_run_preamble(
        db,
        agent=w.pia,
        tenant_id=w.tenant,
        task_text="mach was",
        frame=dict(w.personal.frame or {}),
        model_locality="eu",
        caps=ModelCaps(),
        max_steps=40,
    )
    return pre.has_knowledge


async def test_personal_agent_searches_the_knowledge_of_the_owners_department(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)

        assert await borrowed_kb_sources(db, agent=w.pia) == {w.kb.id: w.dept.id}
        assert w.kb.id in await granted_kb_ids(db, agent=w.pia)
        assert _KB_TEXT in await _search(db, w)


async def test_no_knowledge_without_knowledge_view(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant, knowledge_view=False)

        assert await borrowed_kb_sources(db, agent=w.pia) == {}
        assert _KB_TEXT not in await _search(db, w)


async def test_losing_the_seat_removes_the_knowledge_on_the_next_call(
    app_session: AppSessionFactory,
) -> None:
    """Review Focus 3: the seat is gone between two calls of the same run, so
    the second call no longer finds D's bases, and the next run's preamble no
    longer offers search_knowledge for them."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        assert await _has_knowledge(db, w) is True
        assert _KB_TEXT in await _search(db, w)

        await db.execute(
            update(m.OrgMemberDepartment)
            .where(m.OrgMemberDepartment.id == w.seat.id)
            .values(revoked_at=dt.datetime.now(dt.UTC))
        )
        await db.flush()

        assert _KB_TEXT not in await _search(db, w)
        assert await borrowed_kb_sources(db, agent=w.pia) == {}
        assert await _has_knowledge(db, w) is False


async def test_a_departed_owner_borrows_nothing(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        await db.execute(
            update(m.OrgMember)
            .where(m.OrgMember.id == w.owner.id)
            .values(deleted_at=dt.datetime.now(dt.UTC))
        )
        await db.flush()

        assert await borrowed_kb_sources(db, agent=w.pia) == {}
        assert _KB_TEXT not in await _search(db, w)


async def test_an_archived_department_lends_nothing(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        await db.execute(
            update(m.Department)
            .where(m.Department.id == w.dept.id)
            .values(deleted_at=dt.datetime.now(dt.UTC))
        )
        await db.flush()

        assert await borrowed_kb_sources(db, agent=w.pia) == {}


async def test_no_knowledge_from_personal_or_assistant_departments(
    app_session: AppSessionFactory,
) -> None:
    """Only a standard department lends its bases: not another member's
    personal department, not the Assistant's, even with a seat row in them."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        colleague = await _member(db, tenant, "colleague", await _role(db, tenant))
        theirs = m.Department(
            tenant_id=tenant,
            name="Colleague",
            frame={"closed": True},
            kind=DEPARTMENT_KIND_PERSONAL,
            owner_member_id=colleague.id,
        )
        assistant = m.Department(tenant_id=tenant, name="Assistant", kind=DEPARTMENT_KIND_ASSISTANT)
        db.add_all([theirs, assistant])
        await db.flush()
        private_kb = await _kb(db, tenant, "Privat", "Notizen der Kollegin zur Steuer.")
        assistant_kb = await _kb(db, tenant, "Assistent", "Wissen des Assistenten.")
        own_kb = await _kb(db, tenant, "Eigen", "Wissen der eigenen Abteilung.")
        db.add_all(
            [
                _grant(tenant, private_kb, theirs),
                _grant(tenant, assistant_kb, assistant),
                # The owner's own personal department is not a lending one
                # either: its grants are the agent's own already.
                _grant(tenant, own_kb, w.personal),
                _seat(tenant, w.owner, theirs),
                _seat(tenant, w.owner, assistant),
            ]
        )
        await db.flush()

        assert await borrowed_kb_sources(db, agent=w.pia) == {w.kb.id: w.dept.id}
        assert await granted_kb_ids(db, agent=w.pia) == {w.kb.id, own_kb.id}


async def test_department_memory_of_other_departments_is_never_returned(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        reads_everything = {"memory": {tier: ["read"] for tier in ("agent", "department")}}
        w = await _world(db, tenant, personal_frame={"closed": True, **reads_everything})
        store = m.MemoryStore(tenant_id=tenant, tier="department", owner_id=w.dept.id)
        db.add(store)
        await db.flush()
        db.add(
            m.MemoryRecord(
                tenant_id=tenant,
                store_id=store.id,
                content=_DEPT_MEMORY,
                written_by=uuid.uuid4(),
                status="approved",
            )
        )
        await db.flush()

        recalled = await retrieve_context(
            db,
            agent=w.pia,
            tenant_id=tenant,
            frame=dict(w.personal.frame or {}),
            query_text="Kunde Sued",
        )
        assert _DEPT_MEMORY not in recalled
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=w.pia,
            task=w.task,
            tc=ToolCall(id="m1", name=SEARCH_MEMORY.name, arguments={"query": "Kunde Sued"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
        assert outcome is not None
        assert _DEPT_MEMORY not in outcome.output


_WITH_RESTRICTED = {"cleared_classes": ["public", "internal", "confidential", "restricted"]}
_WITHOUT_RESTRICTED = {"cleared_classes": ["public", "internal", "confidential"]}


@pytest.mark.parametrize(
    ("personal_frame", "dept_frame"),
    [
        ({"closed": True, **_WITHOUT_RESTRICTED}, _WITH_RESTRICTED),
        ({"closed": True, **_WITH_RESTRICTED}, _WITHOUT_RESTRICTED),
    ],
    ids=["personal-frame-stricter", "department-frame-stricter"],
)
async def test_borrowed_knowledge_respects_the_stricter_clearance(
    app_session: AppSessionFactory,
    personal_frame: dict[str, Any],
    dept_frame: dict[str, Any],
) -> None:
    """A restricted chunk in K, read on a local model (restricted text never
    reaches a cloud one): cleared by one frame but not the other, so not
    returned. The unrestricted chunk of the same base still is."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant, personal_frame=personal_frame, dept_frame=dept_frame)
        await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=w.kb.id,
            filename="gehalt.txt",
            content=_RESTRICTED_TEXT,
            content_type="text/plain",
        )
        await db.execute(
            update(m.KbChunk)
            .where(m.KbChunk.kb_id == w.kb.id, m.KbChunk.content.contains("Gehaltsband"))
            .values(classification="restricted")
        )
        await db.flush()

        context, contains_restricted = await retrieve_kb_context(
            db,
            agent=w.pia,
            tenant_id=tenant,
            query_text="Gehaltsband Rahmenvertrag",
            frame=personal_frame,
            model_locality="local",
        )
        assert _KB_TEXT in context
        assert _RESTRICTED_TEXT not in context
        assert contains_restricted is False


async def test_both_frames_clearing_a_restricted_chunk_return_it(
    app_session: AppSessionFactory,
) -> None:
    """The counterpart of the intersection: when both frames clear it, the
    borrowed restricted chunk reaches a local model."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        personal_frame = {"closed": True, **_WITH_RESTRICTED}
        w = await _world(
            db, tenant, personal_frame=personal_frame, dept_frame=dict(_WITH_RESTRICTED)
        )
        await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=w.kb.id,
            filename="gehalt.txt",
            content=_RESTRICTED_TEXT,
            content_type="text/plain",
        )
        await db.execute(
            update(m.KbChunk)
            .where(m.KbChunk.kb_id == w.kb.id, m.KbChunk.content.contains("Gehaltsband"))
            .values(classification="restricted")
        )
        await db.flush()

        context, contains_restricted = await retrieve_kb_context(
            db,
            agent=w.pia,
            tenant_id=tenant,
            query_text="Gehaltsband",
            frame=personal_frame,
            model_locality="local",
        )
        assert _RESTRICTED_TEXT in context
        assert contains_restricted is True


async def test_the_agents_own_grant_wins_over_the_borrowed_one(
    app_session: AppSessionFactory,
) -> None:
    """K granted both to the personal agent itself and to D: the agent's own
    grant decides, with its own frame alone -- D's stricter frame does not
    take away what was granted to the agent directly."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant, dept_frame={"cleared_classes": ["public"]})
        db.add(_grant(tenant, w.kb, w.pia))
        await db.flush()

        assert await borrowed_kb_sources(db, agent=w.pia) == {w.kb.id: w.dept.id}
        context, _ = await retrieve_kb_context(
            db,
            agent=w.pia,
            tenant_id=tenant,
            query_text="Rahmenvertrag",
            frame=dict(w.personal.frame or {}),
        )
        assert _KB_TEXT in context


async def test_d_s_stricter_frame_hides_an_only_borrowed_base(
    app_session: AppSessionFactory,
) -> None:
    """Same world without the agent's own grant: D clears only `public`, the
    chunk is `internal`, so nothing comes back."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant, dept_frame={"cleared_classes": ["public"]})

        context, _ = await retrieve_kb_context(
            db,
            agent=w.pia,
            tenant_id=tenant,
            query_text="Rahmenvertrag",
            frame=dict(w.personal.frame or {}),
        )
        assert context == ""


async def test_search_knowledge_is_offered_when_only_borrowed_bases_exist(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        assert await _has_knowledge(db, w) is True


async def test_standard_agent_knowledge_is_unchanged(app_session: AppSessionFactory) -> None:
    """An agent of D itself: its grants are D's and its own, nothing is
    borrowed, and its frame alone clears what it reads -- even when the owner
    seated in D has a personal department of their own."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        nora = m.Agent(
            tenant_id=tenant,
            department_id=w.dept.id,
            name="Nora",
            status="idle",
            definition={},
            presentation={},
            narrowing={},
        )
        db.add(nora)
        await db.flush()
        mine = await _kb(db, tenant, "Nora", "Noras eigene Notizen zum Einkauf.")
        db.add(_grant(tenant, mine, nora))
        await db.flush()

        assert await borrowed_kb_sources(db, agent=nora) == {}
        assert await granted_kb_ids(db, agent=nora) == {w.kb.id, mine.id}
        context, _ = await retrieve_kb_context(
            db, agent=nora, tenant_id=tenant, query_text="Rahmenvertrag", frame={}
        )
        assert _KB_TEXT in context
