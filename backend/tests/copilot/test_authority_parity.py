"""A follow-up turn resolves exactly the authority of a messenger turn by the
same member (no token claim), which is never more than a web turn."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text

from oc8 import models as m
from oc8.agent import control_tools
from oc8.agent.assistant import get_or_create_assistant
from oc8.authz.scope import AgentActor, DepartmentScope
from oc8.chat.service import send_message
from oc8.copilot.followups import fire_followup, schedule_followup
from oc8.copilot.responsibilities import open_responsibility
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import copilot_seat, once_in_an_hour


async def _bind(db: Any, tenant: uuid.UUID) -> None:
    await db.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant)})


# The scope's department sets are private fields with only partial public accessors
# (`viewable` is the view seats, `_decide` has none); reading them is the only way to
# compare the full reach of two scopes.
def _shape(scope: DepartmentScope) -> tuple[object, ...]:
    return (
        scope.is_unrestricted,
        scope.decides_everywhere,
        scope.viewable,
        scope._decide,
        scope.agent_manage_departments,
    )


def _within(narrow: DepartmentScope, wide: DepartmentScope) -> bool:
    """`narrow` reaches nothing that `wide` does not."""
    sees = wide.is_unrestricted or (not narrow.is_unrestricted and narrow.viewable <= wide.viewable)
    decides = wide.decides_everywhere or (
        not narrow.decides_everywhere and narrow._decide <= wide._decide
    )
    manages = narrow.agent_manage_departments <= wide.agent_manage_departments
    return sees and decides and manages


async def _seat_in_department(db: Any, tenant: uuid.UUID, member_id: uuid.UUID) -> uuid.UUID:
    dept = m.Department(tenant_id=tenant, name="Sales", frame={})
    db.add(dept)
    await db.flush()
    db.add(
        m.OrgMemberDepartment(
            tenant_id=tenant,
            member_id=member_id,
            department_id=dept.id,
            seat_role="dept_approver",
            agent_manage=True,
        )
    )
    await db.flush()
    return dept.id


async def _web_or_messenger_turn(
    app_session: AppSessionFactory, tenant: uuid.UUID, seat: Any, **kw: Any
) -> uuid.UUID:
    async with app_session(tenant) as db:
        await _bind(db, tenant)
        session = await db.get(m.ChatSession, seat.session_id)
        assert session is not None
        _msg, run = await send_message(
            db,
            session=session,
            tenant_id=tenant,
            message="hello",
            originating_operator=seat.subject,
            **kw,
        )
        assert run is not None
        return run.id


async def _settle(app_session: AppSessionFactory, tenant: uuid.UUID, run_id: uuid.UUID) -> None:
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.state = "done"


async def _actor(
    app_session: AppSessionFactory, tenant: uuid.UUID, task_id: uuid.UUID, run_id: uuid.UUID
) -> AgentActor:
    async with app_session(tenant) as db:
        await _bind(db, tenant)
        task = await db.get(m.Task, task_id)
        assert task is not None
        actor = await control_tools._resolve_agent_actor(
            db, tenant_id=tenant, task=task, run_id=run_id
        )
    assert actor is not None
    return actor


async def test_followup_authority_equals_messenger_and_never_exceeds_web(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        seat = await copilot_seat(db, tenant, "lisa@example.com")
        dept_id = await _seat_in_department(db, tenant, seat.member_id)
        r = await open_responsibility(
            db,
            tenant_id=tenant,
            member_id=seat.member_id,
            chat_session_id=seat.session_id,
            title="Offsite",
            goal="Keep the offsite on track",
            origin_channel=None,
            run_id=None,
            actor_agent_id=assistant.id,
            member_subject=seat.subject,
        )
        trigger = await schedule_followup(
            db,
            tenant_id=tenant,
            member_id=seat.member_id,
            responsibility_id=r.id,
            assistant_id=assistant.id,
            spec=once_in_an_hour(),
            prompt="Check the venue replies",
            member_subject=seat.subject,
        )
        trigger_id = trigger.id

    run_a = await _web_or_messenger_turn(
        app_session,
        tenant,
        seat,
        chat_channel="telegram",
        chat_channel_external_id="1",
        operator_role=None,
    )
    # Settle the messenger turn so the follow-up is not skipped as "busy".
    await _settle(app_session, tenant, run_a)
    async with app_session(tenant) as db:
        trig = await db.get(m.Trigger, trigger_id)
        assert trig is not None
        assert await fire_followup(db, trig, tenant_id=tenant) == "fired"
    async with app_session(tenant) as db:
        await _bind(db, tenant)
        runs = (await db.execute(select(m.AgentRun).where(m.AgentRun.id != run_a))).scalars().all()
        followup_runs = [x for x in runs if (x.context or {}).get("door") == "followup"]
        assert len(followup_runs) == 1
        run_b = followup_runs[0].id
        # The follow-up carries no token claim and is attributed to the member.
        assert "operator_role" not in followup_runs[0].context
        assert followup_runs[0].context["originating_operator"] == seat.subject
        assert await control_tools._acting_token_role(db, tenant_id=tenant, run_id=run_b) is None
    await _settle(app_session, tenant, run_b)
    run_c = await _web_or_messenger_turn(app_session, tenant, seat, operator_role="org_admin")

    a = await _actor(app_session, tenant, seat.task_id, run_a)
    b = await _actor(app_session, tenant, seat.task_id, run_b)
    c = await _actor(app_session, tenant, seat.task_id, run_c)
    assert b.member.id == a.member.id == c.member.id == seat.member_id
    # Non-empty on purpose: a vacuous (all-empty) comparison would stay green on a leak.
    assert b.scope.viewable == {dept_id}
    assert b.scope.agent_manage_departments == {dept_id}
    assert _shape(a.scope) == _shape(b.scope)
    assert _within(b.scope, c.scope)


async def test_a_token_claim_widens_a_roleless_members_web_turn(
    app_session: AppSessionFactory,
) -> None:
    """Positive control: where the claim applies (no assigned role) the machinery
    sees a difference, so the parity test above cannot pass by being blind."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seat = await copilot_seat(db, tenant, "tom@example.com", with_role=False)
    run_msg = await _web_or_messenger_turn(
        app_session,
        tenant,
        seat,
        chat_channel="telegram",
        chat_channel_external_id="1",
        operator_role=None,
    )
    await _settle(app_session, tenant, run_msg)
    run_web = await _web_or_messenger_turn(app_session, tenant, seat, operator_role="org_admin")
    msg = await _actor(app_session, tenant, seat.task_id, run_msg)
    web = await _actor(app_session, tenant, seat.task_id, run_web)
    assert _shape(msg.scope) != _shape(web.scope)
    assert _within(msg.scope, web.scope)
    assert not _within(web.scope, msg.scope)
