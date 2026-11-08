"""0109 adds session pins, chat projects and pinned chats."""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_project_and_session_pins_round_trip(app_session: AppSessionFactory) -> None:
    tenant, member = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        project = m.ChatProject(
            tenant_id=tenant,
            owner_member_id=member,
            name="Q4 pricing",
            instructions="Answer in German.",
            pinned_refs=[{"kind": "knowledge_base", "id": "x", "label": "Prices"}],
        )
        db.add(project)
        await db.flush()
        session = m.ChatSession(
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            member_id=member,
            project_id=project.id,
            pinned_refs=[{"kind": "agent", "id": "y", "label": "Lennart"}],
            pinned=True,
        )
        db.add(session)
        await db.flush()
        sid, pid = session.id, project.id
    async with app_session(tenant) as db:
        session = await db.get(m.ChatSession, sid)
        project = await db.get(m.ChatProject, pid)
        assert session is not None and session.pinned and session.project_id == pid
        assert project is not None and project.shared_with == []


async def test_defaults_for_existing_sessions(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        s = m.ChatSession(tenant_id=tenant, agent_id=uuid.uuid4(), member_id=uuid.uuid4())
        db.add(s)
        await db.flush()
        await db.refresh(s)
        assert s.pinned_refs == [] and s.pinned is False and s.project_id is None


async def test_projects_are_tenant_isolated(app_session: AppSessionFactory) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    async with app_session(a) as db:
        db.add(m.ChatProject(tenant_id=a, owner_member_id=uuid.uuid4(), name="mine"))
        await db.flush()
    async with app_session(b) as db:
        rows = (await db.execute(m.ChatProject.__table__.select())).all()
        assert rows == []
