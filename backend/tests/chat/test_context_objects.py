"""Chat context refs for oc8's own objects (oc8 Chat spec §5.1).

Each kind is resolved under the VIEWING member's rights with the rule oc8
already applies to it: the scoped repository for agents and departments, the
Live tab's rule for runs (a colleague's run only for whoever may manage the
agent, and then redacted), the approvals view for approvals, and owner-only
for responsibilities and chats. Whatever the member may not see is dropped --
other-tenant, deleted, malformed and unauthorised read identically."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.permissions import SEAT_VIEWER
from oc8.chat.context.base import Viewer, viewer_for_member
from oc8.chat.context.objects import SUMMARY_CHARS
from oc8.chat.context.registry import RESOLVERS, resolve_ref, resolve_refs
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@dataclass
class World:
    sales: m.Department
    other: m.Department
    seller: m.Agent
    stranger: m.Agent
    admin: Viewer
    emp: Viewer
    sam: Viewer
    boss: Viewer


async def _member(
    db: AsyncSession,
    tenant: uuid.UUID,
    subject: str,
    *,
    token_role: str,
    seat_in: Any = None,
    manages: bool = False,
) -> Viewer:
    row = m.OrgMember(tenant_id=tenant, subject=subject, subject_uuid=uuid.uuid4())
    db.add(row)
    await db.flush()
    if seat_in is not None:
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=row.id,
                department_id=seat_in.id,
                seat_role=SEAT_VIEWER,
                agent_manage=manages,
            )
        )
        await db.flush()
    return await viewer_for_member(db, row, token_role=token_role)


async def _world(db: AsyncSession, tenant: uuid.UUID) -> World:
    sales = m.Department(tenant_id=tenant, name="Sales", frame={}, goal="Close deals")
    other = m.Department(tenant_id=tenant, name="Legal", frame={}, goal="Stay out of court")
    db.add_all([sales, other])
    await db.flush()
    seller = m.Agent(
        tenant_id=tenant,
        department_id=sales.id,
        name="Lennart",
        role_title="Seller",
        mission="Sell. api_key=sk-live-123",
        status="running",
    )
    stranger = m.Agent(tenant_id=tenant, department_id=other.id, name="Lex", role_title="Counsel")
    db.add_all([seller, stranger])
    await db.flush()
    return World(
        sales=sales,
        other=other,
        seller=seller,
        stranger=stranger,
        admin=await _member(db, tenant, "admin", token_role="org_admin"),
        # Copilot only: no seat, no agent:view anywhere.
        emp=await _member(db, tenant, "emp", token_role="member"),
        # A viewer seat in Sales.
        sam=await _member(db, tenant, "sam", token_role="member", seat_in=sales),
        # A viewer seat in Sales that may manage its agents.
        boss=await _member(db, tenant, "boss", token_role="member", seat_in=sales, manages=True),
    )


async def _ids(db: AsyncSession, viewer: Viewer, kind: str, *ids: str) -> list[str]:
    out = await resolve_refs(db, viewer=viewer, refs=[{"kind": kind, "id": i} for i in ids])
    return [r.id for r in out]


async def _summary(db: AsyncSession, viewer: Viewer, kind: str, ref_id: str) -> str:
    resolved = await resolve_ref(db, viewer=viewer, kind=kind, ref_id=ref_id)
    assert resolved is not None
    text = await RESOLVERS[kind].summary(db, resolved[1])
    assert len(text) <= SUMMARY_CHARS
    return text


async def test_every_object_kind_is_registered_on_import() -> None:
    assert {"agent", "department", "run", "approval", "responsibility", "chat_session"} <= set(
        RESOLVERS
    )
    for kind in ("agent", "department", "run", "approval", "responsibility", "chat_session"):
        assert RESOLVERS[kind].readable is False


async def test_object_refs_follow_the_members_visibility(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        session = m.ChatSession(
            tenant_id=tenant, agent_id=w.seller.id, member_id=w.emp.member.id, title="Pricing"
        )
        db.add(session)
        await db.flush()
        db.add(
            m.ChatMessage(
                tenant_id=tenant, session_id=session.id, role="user", content="what about 5%?"
            )
        )
        await db.flush()

        agent_ref = await resolve_ref(db, viewer=w.admin, kind="agent", ref_id=str(w.seller.id))
        assert agent_ref is not None and agent_ref[0].deep_link == f"/agents/{w.seller.id}"
        assert agent_ref[0].classification == "internal"
        assert "Seller" in await RESOLVERS["agent"].summary(db, agent_ref[1])
        assert await resolve_ref(db, viewer=w.emp, kind="agent", ref_id=str(w.seller.id)) is None

        mine = await resolve_ref(db, viewer=w.emp, kind="chat_session", ref_id=str(session.id))
        assert mine is not None
        assert "what about 5%?" in await RESOLVERS["chat_session"].summary(db, mine[1])
        assert (
            await resolve_ref(db, viewer=w.admin, kind="chat_session", ref_id=str(session.id))
            is None
        )


# ----------------------------------------------------------------------- agent


async def test_agent_refs_go_through_the_scoped_repository(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        gone = m.Agent(tenant_id=tenant, department_id=w.sales.id, name="Gone")
        db.add(gone)
        await db.flush()
        gone.deleted_at = datetime.now(tz=UTC)
        await db.flush()
        asked = (
            str(w.seller.id),
            str(w.stranger.id),
            str(gone.id),
            "not-a-uuid",
            str(uuid.uuid4()),
        )
        assert await _ids(db, w.admin, "agent", *asked) == [str(w.seller.id), str(w.stranger.id)]
        # A seat in Sales sees Sales' agent and nothing else.
        assert await _ids(db, w.sam, "agent", *asked) == [str(w.seller.id)]
        assert await _ids(db, w.emp, "agent", *asked) == []

        summary = await _summary(db, w.sam, "agent", str(w.seller.id))
        assert "Lennart" in summary and "running" in summary
        assert "sk-live-123" not in summary
        resolved = await resolve_ref(db, viewer=w.sam, kind="agent", ref_id=str(w.seller.id))
        assert resolved is not None and resolved[0].label == "Lennart"

        found = await RESOLVERS["agent"].search(db, w.sam, "Len", 5)
        assert [RESOLVERS["agent"].label(a) for a in found] == ["Lennart"]
        assert await RESOLVERS["agent"].search(db, w.sam, "Lex", 5) == []
        assert await RESOLVERS["agent"].search(db, w.emp, "Len", 5) == []


async def test_a_member_without_the_door_is_refused_before_any_load(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        for kind in ("agent", "department", "approval"):
            with patch.object(RESOLVERS[kind], "load", side_effect=AssertionError("loaded")):
                assert (
                    await resolve_ref(db, viewer=w.emp, kind=kind, ref_id=str(uuid.uuid4())) is None
                )


# ------------------------------------------------------------------ department


async def test_department_refs_go_through_the_scoped_repository(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        gone = m.Department(tenant_id=tenant, name="Closed", frame={})
        db.add(gone)
        await db.flush()
        gone.deleted_at = datetime.now(tz=UTC)
        await db.flush()
        asked = (str(w.sales.id), str(w.other.id), str(gone.id), "x", str(uuid.uuid4()))
        assert await _ids(db, w.admin, "department", *asked) == [str(w.sales.id), str(w.other.id)]
        assert await _ids(db, w.sam, "department", *asked) == [str(w.sales.id)]
        assert await _ids(db, w.emp, "department", *asked) == []

        resolved = await resolve_ref(db, viewer=w.sam, kind="department", ref_id=str(w.sales.id))
        assert resolved is not None
        assert resolved[0].label == "Sales"
        assert resolved[0].deep_link == f"/departments/{w.sales.id}"
        assert "Close deals" in await RESOLVERS["department"].summary(db, resolved[1])

        found = await RESOLVERS["department"].search(db, w.sam, "a", 5)
        assert [RESOLVERS["department"].label(d) for d in found] == ["Sales"]


# ------------------------------------------------------------------------- run


def _run(tenant: uuid.UUID, agent: m.Agent, *, source: str, **context: Any) -> m.AgentRun:
    return m.AgentRun(
        tenant_id=tenant,
        agent_id=agent.id,
        source=source,
        state="done",
        context={
            "task": f"secret plan of {context.get('originating_operator', 'nobody')}",
            "output": "Offer sent for 4.320 EUR",
            "steps": 2,
            "toolCalls": [
                {"tool": "send_email", "step": 1, "arguments": {"to": "ceo@acme.example"}},
                {"tool": "send_email", "step": 2, "result": "queued id 991"},
            ],
            "pending_questions": [{"q": "hidden question"}],
            **context,
        },
    )


async def test_run_refs_follow_the_live_tab_rule(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        sams_chat = m.ChatSession(tenant_id=tenant, agent_id=w.seller.id, member_id=w.sam.member.id)
        db.add(sams_chat)
        await db.flush()
        own = _run(tenant, w.seller, source="manual", originating_operator="sam")
        autonomous = _run(tenant, w.seller, source="cron")
        in_own_chat = _run(tenant, w.seller, source="chat", chat_session_id=str(sams_chat.id))
        colleagues = _run(tenant, w.seller, source="chat", originating_operator="alice")
        elsewhere = _run(tenant, w.stranger, source="cron")
        gone = _run(tenant, w.seller, source="cron")
        db.add_all([own, autonomous, in_own_chat, colleagues, elsewhere, gone])
        await db.flush()
        await db.delete(gone)
        await db.flush()
        asked = [str(r.id) for r in (own, autonomous, in_own_chat, colleagues, elsewhere, gone)] + [
            "nope",
            str(uuid.uuid4()),
        ]

        # Sam reaches Sales' agent: her own runs, its autonomous runs and the
        # runs of her own chats -- never a colleague's, and nothing of Legal's.
        assert await _ids(db, w.sam, "run", *asked) == [
            str(own.id),
            str(autonomous.id),
            str(in_own_chat.id),
        ]
        # Reaching nothing but the Assistant, the copilot-only member gets none.
        assert await _ids(db, w.emp, "run", *asked) == []
        # The right to manage the agent adds every colleague's run ("All runs"):
        # for Boss, Sam's runs are colleagues' runs too.
        assert await _ids(db, w.boss, "run", *asked) == [
            str(own.id),
            str(autonomous.id),
            str(in_own_chat.id),
            str(colleagues.id),
        ]
        assert "secret plan" not in await _summary(db, w.boss, "run", str(own.id))

        full = await _summary(db, w.sam, "run", str(own.id))
        assert "secret plan of sam" in full and "Offer sent" in full
        assert "hidden question" not in full and "ceo@acme" not in full
        assert "Offer sent" in await _summary(db, w.sam, "run", str(autonomous.id))
        resolved = await resolve_ref(db, viewer=w.sam, kind="run", ref_id=str(own.id))
        assert resolved is not None
        assert resolved[0].deep_link == f"/activity?run={own.id}"
        assert resolved[0].readable is False
        assert await RESOLVERS["run"].search(db, w.sam, "run", 5) == []


async def test_a_colleagues_run_is_redacted(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        colleagues = _run(tenant, w.seller, source="chat", originating_operator="alice")
        db.add(colleagues)
        await db.flush()
        for viewer in (w.boss, w.admin):
            text = await _summary(db, viewer, "run", str(colleagues.id))
            assert "send_email" in text and "done" in text
            for leaked in ("secret plan", "Offer sent", "ceo@acme", "991", "alice", "hidden"):
                assert leaked not in text


async def test_another_tenants_objects_are_dropped(app_session: AppSessionFactory) -> None:
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    async with app_session(theirs) as db:
        w = await _world(db, theirs)
        run = _run(theirs, w.seller, source="cron")
        approval = m.ApprovalRequest(
            tenant_id=theirs,
            agent_id=w.seller.id,
            department_id=w.sales.id,
            action_type="tool_send",
            title="Theirs",
        )
        session = m.ChatSession(tenant_id=theirs, agent_id=w.seller.id, member_id=w.admin.member.id)
        db.add_all([run, approval, session])
        await db.flush()
        resp = m.Responsibility(
            tenant_id=theirs,
            member_id=w.admin.member.id,
            chat_session_id=session.id,
            title="Theirs",
            goal="g",
        )
        db.add(resp)
        await db.flush()
    refs = [
        {"kind": "agent", "id": str(w.seller.id)},
        {"kind": "department", "id": str(w.sales.id)},
        {"kind": "run", "id": str(run.id)},
        {"kind": "approval", "id": str(approval.id)},
        {"kind": "responsibility", "id": str(resp.id)},
        {"kind": "chat_session", "id": str(session.id)},
    ]
    async with app_session(mine) as db:
        here = await _world(db, mine)
        assert await resolve_refs(db, viewer=here.admin, refs=refs) == []


# -------------------------------------------------------------------- approval


async def test_approval_refs_follow_the_approvals_view(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        sales = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=w.seller.id,
            department_id=w.sales.id,
            action_type="tool_send",
            title="Send the offer",
            detail="To ACME, 4.320 EUR",
            amount_text="4.320 EUR",
            payload={"arguments": {"password": "hunter2"}},
        )
        legal = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=w.stranger.id,
            department_id=w.other.id,
            action_type="tool_send",
            title="File the suit",
        )
        gone = m.ApprovalRequest(
            tenant_id=tenant, agent_id=w.seller.id, department_id=w.sales.id, action_type="x"
        )
        db.add_all([sales, legal, gone])
        await db.flush()
        await db.delete(gone)
        await db.flush()
        asked = (str(sales.id), str(legal.id), str(gone.id), "bad", str(uuid.uuid4()))
        assert await _ids(db, w.admin, "approval", *asked) == [str(sales.id), str(legal.id)]
        assert await _ids(db, w.sam, "approval", *asked) == [str(sales.id)]
        assert await _ids(db, w.emp, "approval", *asked) == []

        resolved = await resolve_ref(db, viewer=w.sam, kind="approval", ref_id=str(sales.id))
        assert resolved is not None
        assert resolved[0].label == "Send the offer"
        assert resolved[0].deep_link == "/workspace"
        text = await _summary(db, w.sam, "approval", str(sales.id))
        assert "Send the offer" in text and "pending" in text and "4.320 EUR" in text
        assert "hunter2" not in text


# -------------------------------------------------------------- responsibility


async def test_a_responsibility_is_its_owners_alone(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        session = m.ChatSession(tenant_id=tenant, agent_id=w.seller.id, member_id=w.emp.member.id)
        db.add(session)
        await db.flush()

        def resp(title: str) -> m.Responsibility:
            return m.Responsibility(
                tenant_id=tenant,
                member_id=w.emp.member.id,
                chat_session_id=session.id,
                title=title,
                goal="Get the ACME deal signed",
                next_step="Call on Monday",
            )

        mine, gone = resp("ACME deal"), resp("Old")
        db.add_all([mine, gone])
        await db.flush()
        await db.delete(gone)
        await db.flush()
        asked = (str(mine.id), str(gone.id), "x", str(uuid.uuid4()))
        assert await _ids(db, w.emp, "responsibility", *asked) == [str(mine.id)]
        # Not another member's -- not even an org admin's view of it.
        assert await _ids(db, w.admin, "responsibility", *asked) == []
        assert await _ids(db, w.sam, "responsibility", *asked) == []

        text = await _summary(db, w.emp, "responsibility", str(mine.id))
        assert "ACME deal" in text and "Call on Monday" in text and "active" in text
        found = await RESOLVERS["responsibility"].search(db, w.emp, "acme", 5)
        assert [r.id for r in found] == [mine.id]
        assert await RESOLVERS["responsibility"].search(db, w.admin, "acme", 5) == []


# ---------------------------------------------------------------- chat session


async def test_another_members_chat_is_never_pinnable(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        mine = m.ChatSession(
            tenant_id=tenant, agent_id=w.seller.id, member_id=w.sam.member.id, title="Pricing"
        )
        theirs = m.ChatSession(
            tenant_id=tenant, agent_id=w.seller.id, member_id=w.emp.member.id, title="Pricing too"
        )
        gone = m.ChatSession(tenant_id=tenant, agent_id=w.seller.id, member_id=w.sam.member.id)
        db.add_all([mine, theirs, gone])
        await db.flush()
        await db.delete(gone)
        await db.flush()
        asked = (str(mine.id), str(theirs.id), str(gone.id), "x", str(uuid.uuid4()))
        assert await _ids(db, w.sam, "chat_session", *asked) == [str(mine.id)]
        assert await _ids(db, w.emp, "chat_session", *asked) == [str(theirs.id)]
        assert await _ids(db, w.admin, "chat_session", *asked) == []
        assert await _ids(db, w.boss, "chat_session", *asked) == []

        resolved = await resolve_ref(db, viewer=w.sam, kind="chat_session", ref_id=str(mine.id))
        assert resolved is not None
        assert resolved[0].deep_link == f"/copilot?session={mine.id}"
        found = await RESOLVERS["chat_session"].search(db, w.sam, "pric", 5)
        assert [s.id for s in found] == [mine.id]
        assert await RESOLVERS["chat_session"].search(db, w.admin, "pric", 5) == []


async def test_a_chat_summary_is_compact_and_keeps_the_newest_turn(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        session = m.ChatSession(
            tenant_id=tenant, agent_id=w.seller.id, member_id=w.sam.member.id, title="Long"
        )
        db.add(session)
        await db.flush()
        for i in range(10):
            db.add(
                m.ChatMessage(
                    tenant_id=tenant,
                    session_id=session.id,
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"turn {i} " + "x" * 400,
                    created_at=datetime(2026, 1, 1, 12, i, tzinfo=UTC),
                )
            )
        db.add(
            m.ChatMessage(
                tenant_id=tenant,
                session_id=session.id,
                role="user",
                content="last one, token=abc123",
                created_at=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
            )
        )
        await db.flush()
        text = await _summary(db, w.sam, "chat_session", str(session.id))
        assert text.startswith("Chat 'Long':")
        assert text.endswith("user: last one, token=[redacted]")
        assert "abc123" not in text and "turn 0" not in text
