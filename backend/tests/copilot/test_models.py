from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from oc8 import models as m
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _session(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="A", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Copilot")
        db.add(agent)
        await db.flush()
        member_id = uuid.uuid4()
        s = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=member_id)
        db.add(s)
        await db.flush()
        return s.id, member_id


async def test_responsibility_is_tenant_isolated(app_session: AppSessionFactory) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    session_id, member_id = await _session(app_session, a)
    async with app_session(a) as db:
        db.add(
            m.Responsibility(
                tenant_id=a,
                member_id=member_id,
                chat_session_id=session_id,
                title="Offsite",
                goal="Keep the offsite on track",
            )
        )
    async with app_session(b) as db:
        assert (await db.execute(select(m.Responsibility))).scalars().all() == []


async def test_profile_defaults(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        p = m.CopilotProfile(tenant_id=tenant, member_id=uuid.uuid4())
        db.add(p)
        await db.flush()
        await db.refresh(p)
        assert p.display_name == "Copilot"
        assert p.avatar == {"shape": "round", "color": "indigo"}
        assert p.paused_at is None


async def test_followup_trigger_requires_timezone(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    session_id, _ = await _session(app_session, tenant)
    with pytest.raises(IntegrityError):
        async with app_session(tenant) as db:
            db.add(
                m.Trigger(
                    tenant_id=tenant,
                    agent_id=uuid.uuid4(),
                    kind="once",
                    task_text="x",
                    next_run_at=dt.datetime.now(tz=dt.UTC),
                    chat_session_id=session_id,
                    responsibility_id=uuid.uuid4(),
                    timezone=None,
                )
            )
            await db.flush()


async def test_recurring_followup_requires_end(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    session_id, _ = await _session(app_session, tenant)
    with pytest.raises(IntegrityError):
        async with app_session(tenant) as db:
            db.add(
                m.Trigger(
                    tenant_id=tenant,
                    agent_id=uuid.uuid4(),
                    kind="cron",
                    task_text="x",
                    cron_expression="0 9 * * 1-5",
                    chat_session_id=session_id,
                    responsibility_id=uuid.uuid4(),
                    timezone="Europe/Berlin",
                    ends_at=None,
                )
            )
            await db.flush()


async def test_once_trigger_and_followup_role_are_accepted(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    session_id, _ = await _session(app_session, tenant)
    async with app_session(tenant) as db:
        db.add(
            m.Trigger(
                tenant_id=tenant,
                agent_id=uuid.uuid4(),
                kind="once",
                task_text="x",
                next_run_at=dt.datetime.now(tz=dt.UTC),
                chat_session_id=session_id,
                responsibility_id=uuid.uuid4(),
                timezone="Europe/Berlin",
            )
        )
        db.add(m.ChatMessage(tenant_id=tenant, session_id=session_id, role="followup", content="x"))
        await db.flush()
