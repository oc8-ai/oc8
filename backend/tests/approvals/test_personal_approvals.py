"""A4: a personal agent's approvals are routed to its owner only.

The approvals inbox, the detail load and the decision funnel used to let
anybody unrestricted (`approval:view_any`, `approval:decide_any`, or
`member.all_departments`) see and answer every card in the tenant, a personal
department's included -- with the title, the detail and the held call's
arguments. Ownership is `department.owner_member_id` and no role makes
anybody the owner (`departments.repo.is_private_to`), so no grant overrides
it. The one exception is the `budget_incident` card (ruling R2): spend is
metadata, and it stays with the admins who manage budgets.

The world: `owner` owns a personal department with agent Pia; a standard
"Vertrieb" holds agent Nora. `admin` is an `org_admin` (view_any, decide_any,
budget:manage); `everywhere` is a plain member with `all_departments`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.approvals import NotYourDepartment, decide_approval, raise_approval
from oc8.approvals.repo import load_for_actor, personal_owner_of, visible_approvals
from oc8.auth import get_identity_provider
from oc8.auth.principal import Principal
from oc8.authz.permissions import SEAT_APPROVER
from oc8.authz.scope import HumanActor, scope_for_principal
from oc8.channels import ChannelCapabilities
from oc8.channels import binding as binding_mod
from oc8.channels.base import ApprovalChannel
from oc8.channels.notice import ApprovalNotice
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_SECRET_TITLE = "Pia wants to reply to the owner's private mail"
_SECRET_DETAIL = "the owner's private reason"
_CHANNEL = "fake"


@dataclass(frozen=True)
class _World:
    tenant: uuid.UUID
    owner_member: uuid.UUID
    admin_member: uuid.UUID
    personal: uuid.UUID
    shared: uuid.UUID
    pia: uuid.UUID
    nora: uuid.UUID
    personal_card: uuid.UUID
    budget_card: uuid.UUID
    shared_card: uuid.UUID


def _subject_uuid(subject: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}")


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


_ROLES = {"owner": "member", "admin": "org_admin", "everywhere": "member"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def _card(
    tenant: uuid.UUID, agent: uuid.UUID, department: uuid.UUID, action_type: str, title: str
) -> m.ApprovalRequest:
    return m.ApprovalRequest(
        tenant_id=tenant,
        agent_id=agent,
        department_id=department,
        action_type=action_type,
        status="pending",
        title=title,
        detail=_SECRET_DETAIL if action_type == "tool_send" else "",
        payload={"tool": "mail.reply", "arguments": {"to": "x"}}
        if action_type == "tool_send"
        else {"scope": "department", "department_id": str(department)},
    )


async def _world(app_session: AppSessionFactory) -> _World:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T", settings={}))
        shared = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(shared)
        members: dict[str, m.OrgMember] = {}
        for subject in ("owner", "admin", "everywhere"):
            member = m.OrgMember(
                tenant_id=tenant,
                subject=subject,
                subject_uuid=_subject_uuid(subject),
                display_name=subject.title(),
                all_departments=subject == "everywhere",
            )
            db.add(member)
            members[subject] = member
        await db.flush()
        personal = m.Department(
            tenant_id=tenant,
            name="Owner",
            frame={"closed": True},
            kind="personal",
            owner_member_id=members["owner"].id,
        )
        db.add(personal)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=members["owner"].id,
                department_id=personal.id,
                seat_role=SEAT_APPROVER,
                agent_manage=True,
            )
        )
        nora = m.Agent(tenant_id=tenant, department_id=shared.id, name="Nora")
        pia = m.Agent(tenant_id=tenant, department_id=personal.id, name="Pia")
        db.add_all([nora, pia])
        await db.flush()
        personal_card = _card(tenant, pia.id, personal.id, "tool_send", _SECRET_TITLE)
        budget_card = _card(tenant, pia.id, personal.id, "budget_incident", "Budget exceeded")
        shared_card = _card(tenant, nora.id, shared.id, "tool_send", "Nora wants to call x")
        db.add_all([personal_card, budget_card, shared_card])
        await db.flush()
        return _World(
            tenant=tenant,
            owner_member=members["owner"].id,
            admin_member=members["admin"].id,
            personal=personal.id,
            shared=shared.id,
            pia=pia.id,
            nora=nora.id,
            personal_card=personal_card.id,
            budget_card=budget_card.id,
            shared_card=shared_card.id,
        )


async def _actor(db: Any, w: _World, subject: str) -> HumanActor:
    principal = Principal(subject=subject, tenant_id=w.tenant, role=_ROLES[subject])
    member, scope = await scope_for_principal(db, principal)
    assert member is not None
    return HumanActor(principal=principal, member=member, scope=scope)


async def _status(app_session: AppSessionFactory, w: _World, approval_id: uuid.UUID) -> str:
    async with app_session(w.tenant) as db:
        row = (
            await db.execute(
                select(m.ApprovalRequest.status).where(m.ApprovalRequest.id == approval_id)
            )
        ).scalar_one_or_none()
        assert row is not None, "the approval row must be kept"
        return row


# ------------------------------------------------------------------ inbox, detail, decision


@pytest.mark.parametrize("subject", ["admin", "everywhere"])
async def test_admin_with_decide_any_neither_sees_nor_decides_a_personal_approval(
    app_session: AppSessionFactory, subject: str
) -> None:
    w = await _world(app_session)
    headers = _headers(w.tenant, subject, _ROLES[subject])
    async with _http() as http:
        listed = await http.get("/api/v1/approvals", headers=headers)
        assert listed.status_code == 200, listed.text
        ids = {row["id"] for row in listed.json()}
        assert str(w.personal_card) not in ids
        assert _SECRET_TITLE not in listed.text
        # The control: the same caller does see the standard department's card.
        assert str(w.shared_card) in ids

        narrowed = await http.get(
            "/api/v1/approvals", params={"departmentId": str(w.personal)}, headers=headers
        )
        assert narrowed.status_code == 200, narrowed.text
        assert str(w.personal_card) not in {row["id"] for row in narrowed.json()}

        refused = await http.post(
            f"/api/v1/approvals/{w.personal_card}/decision",
            json={"decision": "approve"},
            headers=headers,
        )
        assert refused.status_code == 404, refused.text

    # The detail load (chat object refs, the messenger door, the Copilot tool).
    async with app_session(w.tenant) as db:
        actor = await _actor(db, w, subject)
        assert await load_for_actor(db, w.personal_card, actor=actor) is None
        # And the funnel itself, with the door taken out of the picture:
        # `decide_everywhere` does not override ownership.
        row = (
            await db.execute(
                select(m.ApprovalRequest).where(m.ApprovalRequest.id == w.personal_card)
            )
        ).scalar_one()
        with pytest.raises(NotYourDepartment):
            await decide_approval(db, row, decision="approve", tenant_id=w.tenant, actor=actor)

    assert await _status(app_session, w, w.personal_card) == "pending"


async def test_owner_sees_and_decides_their_agents_approval(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    headers = _headers(w.tenant, "owner", "member")
    async with _http() as http:
        listed = await http.get("/api/v1/approvals", headers=headers)
        assert listed.status_code == 200, listed.text
        ids = {row["id"] for row in listed.json()}
        assert str(w.personal_card) in ids
        assert _SECRET_TITLE in listed.text
        assert str(w.shared_card) not in ids

        async with app_session(w.tenant) as db:
            actor = await _actor(db, w, "owner")
            assert await load_for_actor(db, w.personal_card, actor=actor) is not None

        decided = await http.post(
            f"/api/v1/approvals/{w.personal_card}/decision",
            json={"decision": "reject"},
            headers=headers,
        )
        assert decided.status_code == 200, decided.text

    assert await _status(app_session, w, w.personal_card) == "rejected"


async def test_budget_incident_of_a_personal_department_stays_with_admins(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    headers = _headers(w.tenant, "admin", "org_admin")
    async with _http() as http:
        listed = await http.get("/api/v1/approvals", headers=headers)
        assert listed.status_code == 200, listed.text
        assert str(w.budget_card) in {row["id"] for row in listed.json()}

        async with app_session(w.tenant) as db:
            actor = await _actor(db, w, "admin")
            assert await load_for_actor(db, w.budget_card, actor=actor) is not None

        decided = await http.post(
            f"/api/v1/approvals/{w.budget_card}/decision",
            json={"decision": "reject"},
            headers=headers,
        )
        assert decided.status_code == 200, decided.text

    assert await _status(app_session, w, w.budget_card) == "rejected"


# ------------------------------------------------------------------ announcing


@dataclass
class _SpyBus:
    tenant_events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    member_events: list[tuple[list[uuid.UUID], str, dict[str, Any]]] = field(default_factory=list)

    async def publish_event(
        self, tenant_id: uuid.UUID, type_: str, data: dict[str, Any], *, source: str
    ) -> None:
        self.tenant_events.append((type_, data))

    async def publish_member_event(
        self,
        tenant_id: uuid.UUID,
        member_ids: list[uuid.UUID],
        type_: str,
        data: dict[str, Any],
        *,
        source: str,
    ) -> None:
        self.member_events.append((list(member_ids), type_, data))


@dataclass
class _FakeChannel:
    channel_id: str = _CHANNEL
    delivered: list[str] = field(default_factory=list)

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(max_classification="internal")

    async def deliver(self, notice: ApprovalNotice, *, external_id: str) -> str | None:
        self.delivered.append(external_id)
        return f"msg-{external_id}"

    async def withdraw(self, *a: Any, **k: Any) -> None:
        return None


async def _bind_phones(app_session: AppSessionFactory, w: _World) -> None:
    async with app_session(w.tenant) as db:
        members = {
            row.subject: row.id
            for row in (
                await db.execute(select(m.OrgMember).where(m.OrgMember.tenant_id == w.tenant))
            ).scalars()
        }
        for subject in ("owner", "admin", "everywhere"):
            db.add(
                m.ApprovalChannelBinding(
                    tenant_id=w.tenant,
                    channel=_CHANNEL,
                    user_id=_subject_uuid(subject),
                    external_id=f"phone-{subject}",
                    member_id=members[subject],
                )
            )
        await db.flush()


def _spy(monkeypatch: pytest.MonkeyPatch) -> tuple[_SpyBus, _FakeChannel]:
    bus = _SpyBus()
    channel = _FakeChannel()
    monkeypatch.setattr("oc8.realtime.bus.get_event_bus", lambda: bus)

    async def _channels(db: Any, *, tenant_id: uuid.UUID) -> dict[str, ApprovalChannel]:
        return {_CHANNEL: cast(ApprovalChannel, channel)}

    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", _channels)
    return bus, channel


async def test_personal_approval_is_announced_to_the_owner_only(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(app_session)
    await _bind_phones(app_session, w)
    bus, channel = _spy(monkeypatch)

    async with app_session(w.tenant) as db:
        run = m.AgentRun(tenant_id=w.tenant, agent_id=w.pia, state="running", context={})
        db.add(run)
        await db.flush()
        pia = (await db.execute(select(m.Agent).where(m.Agent.id == w.pia))).scalar_one()

        # Through a real direct publisher: the isolated runtime's held call.
        from oc8.api.v1.internal_agent import _park_held_call

        await _park_held_call(
            db,
            run=run,
            agent=pia,
            ctx={},
            pending={},
            tool="mail.reply",
            arguments={"to": "x"},
            reason=_SECRET_DETAIL,
        )

    created = [e for e in bus.member_events if e[1] == "approval.created"]
    assert len(created) == 1, bus.member_events
    assert created[0][0] == [w.owner_member]
    assert not [e for e in bus.tenant_events if e[0] == "approval.created"], bus.tenant_events
    # The messenger fan-out: the owner's phone alone, never an all_departments one.
    assert channel.delivered == ["phone-owner"]

    async with app_session(w.tenant) as db:
        people = await binding_mod.recipients(
            db,
            tenant_id=w.tenant,
            channel=_CHANNEL,
            department_id=w.personal,
            action_type="tool_send",
        )
        assert [p.external_id for p in people] == ["phone-owner"]
        # R2: the budget incident of a personal department is still the admins'.
        budget = await binding_mod.recipients(
            db,
            tenant_id=w.tenant,
            channel=_CHANNEL,
            department_id=w.personal,
            action_type="budget_incident",
        )
        assert {p.external_id for p in budget} == {"phone-owner", "phone-everywhere"}


async def test_a_standard_approval_is_still_announced_tenant_wide(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the test above."""
    w = await _world(app_session)
    bus, _channel = _spy(monkeypatch)
    async with app_session(w.tenant) as db:
        run = m.AgentRun(tenant_id=w.tenant, agent_id=w.nora, state="running", context={})
        db.add(run)
        await db.flush()
        nora = (await db.execute(select(m.Agent).where(m.Agent.id == w.nora))).scalar_one()
        from oc8.api.v1.internal_agent import _park_held_call

        await _park_held_call(
            db,
            run=run,
            agent=nora,
            ctx={},
            pending={},
            tool="crm.write",
            arguments={},
            reason="",
        )
    assert [e for e in bus.tenant_events if e[0] == "approval.created"]
    assert not bus.member_events


# ------------------------------------------------------------------ the owner leaves


async def test_a_departed_owners_approval_is_seen_by_nobody_but_kept(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(app_session)
    await _bind_phones(app_session, w)
    async with app_session(w.tenant) as db:
        owner = (
            await db.execute(select(m.OrgMember).where(m.OrgMember.id == w.owner_member))
        ).scalar_one()
        owner.deleted_at = dt.datetime.now(dt.UTC)
        await db.flush()

    bus, channel = _spy(monkeypatch)
    async with app_session(w.tenant) as db:
        raised = await raise_approval(
            db,
            tenant_id=w.tenant,
            agent_id=w.pia,
            action_type="tool_send",
            title=_SECRET_TITLE,
            detail=_SECRET_DETAIL,
        )
        raised_id = raised.id
    # Nobody is told: no member, no tenant event, no phone.
    assert not bus.member_events
    assert not bus.tenant_events
    assert channel.delivered == []

    headers = _headers(w.tenant, "admin", "org_admin")
    async with _http() as http:
        listed = await http.get("/api/v1/approvals", headers=headers)
        assert listed.status_code == 200, listed.text
        ids = {row["id"] for row in listed.json()}
        assert str(w.personal_card) not in ids
        assert str(raised_id) not in ids
        for card in (w.personal_card, raised_id):
            refused = await http.post(
                f"/api/v1/approvals/{card}/decision",
                json={"decision": "approve"},
                headers=headers,
            )
            assert refused.status_code == 404, refused.text

    assert await _status(app_session, w, w.personal_card) == "pending"
    assert await _status(app_session, w, raised_id) == "pending"


# ------------------------------------------------------------------ the rule itself


async def test_personal_owner_of_names_the_owner_except_for_budget_incidents(
    app_session: AppSessionFactory,
) -> None:
    owner = uuid.uuid4()
    card = m.ApprovalRequest(action_type="tool_send")
    budget = m.ApprovalRequest(action_type="budget_incident")
    assert personal_owner_of(card, department_kind="personal", owner_member_id=owner) == owner
    assert personal_owner_of(card, department_kind="standard", owner_member_id=owner) is None
    assert personal_owner_of(budget, department_kind="personal", owner_member_id=owner) is None


async def test_the_copilot_list_tool_reads_through_the_same_rule(
    app_session: AppSessionFactory,
) -> None:
    """`list_pending_approvals` calls `visible_approvals` with the member's
    actor; the admin's listing leaves the personal card out there too."""
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        actor = await _actor(db, w, "admin")
        rows = await visible_approvals(db, actor=actor, status="pending")
        ids = {r.id for r in rows}
        assert w.personal_card not in ids
        assert {w.shared_card, w.budget_card} <= ids
