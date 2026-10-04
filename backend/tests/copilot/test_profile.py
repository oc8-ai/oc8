from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.copilot.profile import (
    ProfileError,
    computed_status,
    get_or_create_profile,
    offboard_member,
    pause,
    resume,
    update_profile,
)
from tests.conftest import AppSessionFactory


async def test_profile_is_created_once(app_session: AppSessionFactory) -> None:
    tenant, member = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        a = await get_or_create_profile(db, tenant_id=tenant, member_id=member)
        b = await get_or_create_profile(db, tenant_id=tenant, member_id=member)
        assert a.id == b.id and a.display_name == "Copilot"


async def test_profile_validation(app_session: AppSessionFactory) -> None:
    tenant, member = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        with pytest.raises(ProfileError):
            await update_profile(
                db, tenant_id=tenant, member_id=member, display_name="x" * 41, avatar=None
            )
        with pytest.raises(ProfileError):
            await update_profile(
                db,
                tenant_id=tenant,
                member_id=member,
                display_name=None,
                avatar={"shape": "hexagon", "color": "indigo"},
            )
        p = await update_profile(
            db,
            tenant_id=tenant,
            member_id=member,
            display_name="Alfred",
            avatar={"shape": "star", "color": "teal"},
        )
        assert p.display_name == "Alfred" and p.avatar == {"shape": "star", "color": "teal"}


async def test_status_order(app_session: AppSessionFactory) -> None:
    tenant, member = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        kw = {"tenant_id": tenant, "member_id": member, "assistant_id": assistant.id}
        assert (await computed_status(db, **kw))[0] == "ready"
        await pause(db, tenant_id=tenant, member_id=member)
        assert (await computed_status(db, **kw))[0] == "paused"
        await resume(db, tenant_id=tenant, member_id=member)
        assert (await computed_status(db, **kw))[0] == "ready"


async def test_offboarding_removes_personal_layer(app_session: AppSessionFactory) -> None:
    tenant, member = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        await get_or_create_profile(db, tenant_id=tenant, member_id=member)
        resp = m.Responsibility(
            tenant_id=tenant,
            member_id=member,
            chat_session_id=uuid.uuid4(),
            title="t",
            goal="g",
        )
        db.add(resp)
        await db.flush()
        trigger = m.Trigger(
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            kind="once",
            task_text="check in",
            enabled=True,
            next_run_at=dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=1),
            chat_session_id=resp.chat_session_id,
            responsibility_id=resp.id,
            timezone="UTC",
        )
        db.add(trigger)
        await db.flush()
        await offboard_member(db, tenant_id=tenant, member_id=member)
        await db.refresh(trigger)
        assert trigger.enabled is False
        assert (await db.execute(select(m.CopilotProfile))).scalars().all() == []
        states = [r.state for r in (await db.execute(select(m.Responsibility))).scalars()]
        assert states == ["cancelled"]


async def _run(
    db: object, tenant: uuid.UUID, agent: uuid.UUID, session: uuid.UUID, state: str
) -> m.AgentRun:
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=agent,
        state=state,
        source="chat",
        context={"chat_session_id": str(session)},
    )
    db.add(run)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    return run


async def test_status_working_waiting_and_pause_cancels_runs(
    app_session: AppSessionFactory,
) -> None:
    tenant, member = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        session = m.ChatSession(tenant_id=tenant, agent_id=assistant.id, member_id=member)
        db.add(session)
        await db.flush()
        kw = {"tenant_id": tenant, "member_id": member, "assistant_id": assistant.id}
        running = await _run(db, tenant, assistant.id, session.id, "running")
        assert await computed_status(db, **kw) == ("working", 1)
        await _run(db, tenant, assistant.id, session.id, "waiting_for_approval")
        assert await computed_status(db, **kw) == ("waiting", 1)

        await pause(db, tenant_id=tenant, member_id=member)
        await pause(db, tenant_id=tenant, member_id=member)  # idempotent
        cancelled = (await db.execute(select(m.RunCancellation))).scalars().all()
        assert [c.run_id for c in cancelled] == [running.id]


async def test_pause_and_status_are_member_scoped(app_session: AppSessionFactory) -> None:
    tenant, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        sess_a = m.ChatSession(tenant_id=tenant, agent_id=assistant.id, member_id=a)
        sess_b = m.ChatSession(tenant_id=tenant, agent_id=assistant.id, member_id=b)
        db.add_all([sess_a, sess_b])
        await db.flush()
        run_a = await _run(db, tenant, assistant.id, sess_a.id, "running")
        run_b = await _run(db, tenant, assistant.id, sess_b.id, "running")
        await _run(db, tenant, assistant.id, sess_b.id, "queued")

        await pause(db, tenant_id=tenant, member_id=a)

        cancelled = {c.run_id for c in (await db.execute(select(m.RunCancellation))).scalars()}
        assert cancelled == {run_a.id}
        assert run_b.id not in cancelled
        status_b = await computed_status(
            db, tenant_id=tenant, member_id=b, assistant_id=assistant.id
        )
        assert status_b == ("working", 2)
        await resume(db, tenant_id=tenant, member_id=a)
        status_a = await computed_status(
            db, tenant_id=tenant, member_id=a, assistant_id=assistant.id
        )
        assert status_a == ("working", 1)
