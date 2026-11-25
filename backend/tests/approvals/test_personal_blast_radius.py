"""Ruling U3: a personal agent's blast-radius alarm is decided by its owner AND
the admins.

A `blast_radius` card stops a run that changed too many records. Deciding one
needs `approval:decide_any` (it is not in `EFFECT_PERMISSIONS`), which the
owner of a personal department does not hold -- while A4 refused every admin
any personal card. Nobody could answer it. Now it is an `ADMIN_KEPT_ACTIONS`
card like the budget incident (its title, count, entity and record reference
are metadata, R2), and the department's owner may also apply its effect in
their OWN personal department. Standard departments and every other card are
unchanged.

The world is `test_personal_approvals`': `owner` owns a personal department
with agent Pia; "Vertrieb" holds Nora; `admin` is an `org_admin`;
`everywhere` is a plain member with `all_departments`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.approvals import NotYourSayAtAll, decide_approval
from oc8.approvals.repo import load_for_actor, visible_approvals
from oc8.auth.principal import Principal
from oc8.authz.permissions import SEAT_APPROVER
from oc8.authz.scope import HumanActor, scope_for_principal
from tests.approvals.test_personal_approvals import (
    _actor,
    _bind_phones,
    _headers,
    _http,
    _spy,
    _status,
    _subject_uuid,
    _World,
    _world,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_ALARM_TITLE = "Pia changed 60 records in one run"


async def _blast_card(
    app_session: AppSessionFactory, w: _World, *, agent: uuid.UUID, department: uuid.UUID
) -> uuid.UUID:
    async with app_session(w.tenant) as db:
        card = m.ApprovalRequest(
            tenant_id=w.tenant,
            agent_id=agent,
            department_id=department,
            action_type="blast_radius",
            status="pending",
            title=_ALARM_TITLE,
            detail="60 of 50 res.partner records, last 42",
            payload={"entity": "res.partner", "record": "42", "touched": 60, "limit": 50},
        )
        db.add(card)
        await db.flush()
        return card.id


async def _row(db: Any, approval_id: uuid.UUID) -> m.ApprovalRequest:
    return (
        await db.execute(select(m.ApprovalRequest).where(m.ApprovalRequest.id == approval_id))
    ).scalar_one()


# ------------------------------------------------------------------ the owner


async def test_the_owner_sees_and_decides_their_agents_blast_radius_alarm(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    by_http = await _blast_card(app_session, w, agent=w.pia, department=w.personal)
    by_funnel = await _blast_card(app_session, w, agent=w.pia, department=w.personal)
    headers = _headers(w.tenant, "owner", "member")
    async with _http() as http:
        listed = await http.get("/api/v1/approvals", headers=headers)
        assert listed.status_code == 200, listed.text
        assert str(by_http) in {row["id"] for row in listed.json()}

        decided = await http.post(
            f"/api/v1/approvals/{by_http}/decision",
            json={"decision": "approve"},
            headers=headers,
        )
        assert decided.status_code == 200, decided.text
    assert await _status(app_session, w, by_http) == "approved"

    async with app_session(w.tenant) as db:
        actor = await _actor(db, w, "owner")
        assert await load_for_actor(db, by_funnel, actor=actor) is not None
        await decide_approval(
            db, await _row(db, by_funnel), decision="reject", tenant_id=w.tenant, actor=actor
        )
    assert await _status(app_session, w, by_funnel) == "rejected"


async def test_the_owner_still_cannot_decide_a_personal_budget_incident(
    app_session: AppSessionFactory,
) -> None:
    """The owner's exception is the blast-radius effect only: lifting a budget
    stays `budget:manage`."""
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        actor = await _actor(db, w, "owner")
        with pytest.raises(NotYourSayAtAll):
            await decide_approval(
                db,
                await _row(db, w.budget_card),
                decision="approve",
                tenant_id=w.tenant,
                actor=actor,
            )
    assert await _status(app_session, w, w.budget_card) == "pending"


# ------------------------------------------------------------------ the admins


async def test_an_admin_sees_and_decides_a_personal_blast_radius_alarm(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    card = await _blast_card(app_session, w, agent=w.pia, department=w.personal)
    headers = _headers(w.tenant, "admin", "org_admin")
    async with _http() as http:
        listed = await http.get("/api/v1/approvals", headers=headers)
        assert listed.status_code == 200, listed.text
        assert str(card) in {row["id"] for row in listed.json()}

        async with app_session(w.tenant) as db:
            actor = await _actor(db, w, "admin")
            assert await load_for_actor(db, card, actor=actor) is not None
            ids = {r.id for r in await visible_approvals(db, actor=actor, status="pending")}
            assert card in ids
            # The rest of the department stays private.
            assert w.personal_card not in ids

        decided = await http.post(
            f"/api/v1/approvals/{card}/decision",
            json={"decision": "approve"},
            headers=headers,
        )
        assert decided.status_code == 200, decided.text
    assert await _status(app_session, w, card) == "approved"


async def test_all_departments_without_decide_any_cannot_decide_it(
    app_session: AppSessionFactory,
) -> None:
    """`all_departments` lets a member SEE the card (it is an admin-kept one)
    but not apply its effect, which is `approval:decide_any`."""
    w = await _world(app_session)
    card = await _blast_card(app_session, w, agent=w.pia, department=w.personal)
    headers = _headers(w.tenant, "everywhere", "member")
    async with _http() as http:
        refused = await http.post(
            f"/api/v1/approvals/{card}/decision",
            json={"decision": "approve"},
            headers=headers,
        )
        assert refused.status_code == 403, refused.text
    async with app_session(w.tenant) as db:
        actor = await _actor(db, w, "everywhere")
        with pytest.raises(NotYourSayAtAll):
            await decide_approval(
                db, await _row(db, card), decision="approve", tenant_id=w.tenant, actor=actor
            )
    assert await _status(app_session, w, card) == "pending"


# ------------------------------------------------------------------ standard departments


async def test_a_standard_departments_blast_radius_alarm_is_unchanged(
    app_session: AppSessionFactory,
) -> None:
    """A seat in a standard department sees the alarm but cannot decide it (it
    needs `approval:decide_any`); an admin can."""
    w = await _world(app_session)
    card = await _blast_card(app_session, w, agent=w.nora, department=w.shared)
    async with app_session(w.tenant) as db:
        seated = m.OrgMember(
            tenant_id=w.tenant,
            subject="seated",
            subject_uuid=_subject_uuid("seated"),
            display_name="Seated",
        )
        db.add(seated)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=w.tenant,
                member_id=seated.id,
                department_id=w.shared,
                seat_role=SEAT_APPROVER,
            )
        )
        await db.flush()

    async with app_session(w.tenant) as db:
        principal = Principal(subject="seated", tenant_id=w.tenant, role="member")
        member, scope = await scope_for_principal(db, principal)
        assert member is not None
        actor = HumanActor(principal=principal, member=member, scope=scope)
        assert await load_for_actor(db, card, actor=actor) is not None
        with pytest.raises(NotYourSayAtAll):
            await decide_approval(
                db, await _row(db, card), decision="approve", tenant_id=w.tenant, actor=actor
            )
        # Nor does owning a personal department elsewhere help.
        owner = await _actor(db, w, "owner")
        assert await load_for_actor(db, card, actor=owner) is None

    async with app_session(w.tenant) as db:
        admin = await _actor(db, w, "admin")
        await decide_approval(
            db, await _row(db, card), decision="approve", tenant_id=w.tenant, actor=admin
        )
    assert await _status(app_session, w, card) == "approved"


# ------------------------------------------------------------------ announcing


async def test_a_personal_blast_radius_alarm_reaches_the_admins_and_the_owner(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real publisher: tenant-wide like the budget incident, plus
    the owner's own sockets (a member without the tenant feed hears nothing
    tenant-wide), and on the messenger the owner's seat and `all_departments`."""
    w = await _world(app_session)
    await _bind_phones(app_session, w)
    bus, channel = _spy(monkeypatch)
    async with app_session(w.tenant) as db:
        run = m.AgentRun(tenant_id=w.tenant, agent_id=w.pia, state="running", context={})
        db.add(run)
        await db.flush()
        pia = (await db.execute(select(m.Agent).where(m.Agent.id == w.pia))).scalar_one()
        from oc8.api.mcp_gateway import _stop_for_blast_radius

        await _stop_for_blast_radius(
            db, run=run, agent=pia, entity="res.partner", ref="42", touched=60, limit=50
        )

    created = [e for e in bus.tenant_events if e[0] == "approval.created"]
    assert len(created) == 1, bus.tenant_events
    assert created[0][1]["action_type"] == "blast_radius"
    owner_events = [e for e in bus.member_events if e[1] == "approval.created"]
    assert [e[0] for e in owner_events] == [[w.owner_member]]
    assert sorted(channel.delivered) == ["phone-everywhere", "phone-owner"]
