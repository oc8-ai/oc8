from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.copilot.responsibilities import (
    ResponsibilityError,
    can_transition,
    close_responsibility,
    list_responsibilities,
    open_responsibility,
    update_responsibility,
)
from tests.conftest import AppSessionFactory


async def _open(
    db: AsyncSession,
    tenant: uuid.UUID,
    member: uuid.UUID,
    session_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> m.Responsibility:
    return await open_responsibility(
        db,
        tenant_id=tenant,
        member_id=member,
        chat_session_id=session_id,
        title="Offsite",
        goal="Keep the offsite on track",
        origin_channel=None,
        run_id=None,
        actor_agent_id=agent_id,
        member_subject="op@example.com",
    )


def test_transitions() -> None:
    assert can_transition("active", "waiting")
    assert can_transition("paused", "active")
    assert not can_transition("done", "active")
    assert not can_transition("paused", "waiting")


@pytest.mark.asyncio
async def test_open_requires_goal(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        with pytest.raises(ResponsibilityError, match="goal"):
            await open_responsibility(
                db,
                tenant_id=tenant,
                member_id=uuid.uuid4(),
                chat_session_id=uuid.uuid4(),
                title="x",
                goal="  ",
                origin_channel=None,
                run_id=None,
                actor_agent_id=uuid.uuid4(),
                member_subject="op",
            )


@pytest.mark.asyncio
async def test_open_writes_audit_event(app_session: AppSessionFactory) -> None:
    tenant, member, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        r = await _open(db, tenant, member, uuid.uuid4(), agent_id)
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.action == "copilot.responsibility_opened"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [e.resource["responsibility_id"] for e in events] == [str(r.id)]


@pytest.mark.asyncio
async def test_other_member_cannot_update(app_session: AppSessionFactory) -> None:
    tenant, member = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        r = await _open(db, tenant, member, uuid.uuid4(), uuid.uuid4())
        with pytest.raises(ResponsibilityError, match="not found"):
            await update_responsibility(
                db, tenant_id=tenant, member_id=uuid.uuid4(), responsibility_id=r.id, next_step="x"
            )


@pytest.mark.asyncio
async def test_report_records_run(app_session: AppSessionFactory) -> None:
    tenant, member, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        r = await _open(db, tenant, member, uuid.uuid4(), uuid.uuid4())
        r = await update_responsibility(
            db,
            tenant_id=tenant,
            member_id=member,
            responsibility_id=r.id,
            next_step="Call venue",
            report=True,
            run_id=run_id,
        )
        assert r.last_report_run_id == run_id and r.next_step == "Call venue"
        assert r.last_update_at is not None


@pytest.mark.asyncio
async def test_closing_disables_followups(app_session: AppSessionFactory) -> None:
    tenant, member, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session_id = uuid.uuid4()
    async with app_session(tenant) as db:
        r = await _open(db, tenant, member, session_id, agent_id)
        t = m.Trigger(
            tenant_id=tenant,
            agent_id=agent_id,
            kind="once",
            task_text="x",
            next_run_at=dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=1),
            chat_session_id=session_id,
            responsibility_id=r.id,
            timezone="UTC",
        )
        db.add(t)
        await db.flush()
        await close_responsibility(
            db,
            tenant_id=tenant,
            member_id=member,
            responsibility_id=r.id,
            state="done",
            reason="offsite happened",
            actor_agent_id=None,
            member_subject="op",
        )
        await db.refresh(t)
        assert t.enabled is False
        assert [
            x.id
            for x in await list_responsibilities(
                db, tenant_id=tenant, member_id=member, states=["active"]
            )
        ] == []


@pytest.mark.asyncio
async def test_cannot_update_terminal_responsibility(app_session: AppSessionFactory) -> None:
    tenant, member = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        r = await _open(db, tenant, member, uuid.uuid4(), uuid.uuid4())
        await close_responsibility(
            db,
            tenant_id=tenant,
            member_id=member,
            responsibility_id=r.id,
            state="done",
            reason="test",
            actor_agent_id=None,
            member_subject="op",
        )
        with pytest.raises(ResponsibilityError, match="the responsibility is done"):
            await update_responsibility(
                db,
                tenant_id=tenant,
                member_id=member,
                responsibility_id=r.id,
                next_step="x",
            )
