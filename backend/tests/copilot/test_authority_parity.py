"""A follow-up turn resolves exactly the authority of a messenger turn by the
same member (no token claim), which is never more than a web turn."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text

from oc8 import models as m
from oc8.agent import control_tools
from oc8.agent.assistant import get_or_create_assistant
from oc8.authz.scope import DepartmentScope
from oc8.chat.service import send_message
from oc8.copilot.followups import fire_followup, schedule_followup
from oc8.copilot.responsibilities import open_responsibility
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import copilot_seat, once_in_an_hour


async def _bind(db: Any, tenant: uuid.UUID) -> None:
    await db.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant)})


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


async def test_followup_authority_equals_messenger_and_never_exceeds_web(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        seat = await copilot_seat(db, tenant, "lisa@example.com")
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

    async def _turn(**kw: Any) -> uuid.UUID:
        async with app_session(tenant) as db:
            await _bind(db, tenant)
            session = await db.get(m.ChatSession, seat.session_id)
            assert session is not None
            _msg, run = await send_message(
                db,
                session=session,
                tenant_id=tenant,
                message="hello",
                originating_operator=kw.pop("originating_operator", seat.subject),
                **kw,
            )
            assert run is not None
            return run.id

    run_a = await _turn(chat_channel="telegram", chat_channel_external_id="1", operator_role=None)
    async with app_session(tenant) as db:
        # Settle the messenger turn so the follow-up is not skipped as "busy".
        run = await db.get(m.AgentRun, run_a)
        assert run is not None
        run.state = "done"
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
        run = await db.get(m.AgentRun, run_b)
        assert run is not None
        run.state = "done"
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_a)
        assert run is not None
        run.state = "done"
    run_c = await _turn(operator_role="org_admin")

    actors = {}
    async with app_session(tenant) as db:
        await _bind(db, tenant)
        task = await db.get(m.Task, seat.task_id)
        assert task is not None
        for name, rid in (("a", run_a), ("b", run_b), ("c", run_c)):
            actors[name] = await control_tools._resolve_agent_actor(
                db, tenant_id=tenant, task=task, run_id=rid
            )
    a, b, c = actors["a"], actors["b"], actors["c"]
    assert a is not None and b is not None and c is not None
    assert b.member.id == a.member.id == c.member.id == seat.member_id
    assert _shape(a.scope) == _shape(b.scope)
    assert _within(b.scope, c.scope)
