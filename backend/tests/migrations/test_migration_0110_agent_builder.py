"""0110 adds the agent builder's proposal columns and a session's sticky mode."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from oc8 import models as m
from oc8.copilot.models import CopilotProposal
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _builder(tenant: uuid.UUID, session_id: uuid.UUID, status: str) -> CopilotProposal:
    return CopilotProposal(
        tenant_id=tenant,
        created_by="emp",
        kind="agent_builder",
        session_id=session_id,
        status=status,
    )


async def test_a_builder_proposal_round_trips(app_session: AppSessionFactory) -> None:
    tenant, session_id = uuid.uuid4(), uuid.uuid4()
    expires = dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=7)
    async with app_session(tenant) as db:
        p = CopilotProposal(
            tenant_id=tenant,
            created_by="emp",
            kind="agent_builder",
            session_id=session_id,
            expires_at=expires,
            status="awaiting_approval",
            result={"approvalId": "x"},
        )
        db.add(p)
        await db.flush()
        pid = p.id
    async with app_session(tenant) as db:
        loaded = await db.get(CopilotProposal, pid)
        assert loaded is not None
        assert loaded.kind == "agent_builder" and loaded.session_id == session_id
        assert loaded.expires_at == expires
        assert loaded.status == "awaiting_approval" and loaded.result == {"approvalId": "x"}


async def test_existing_proposals_default_to_operations(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        p = CopilotProposal(tenant_id=tenant, created_by="admin")
        db.add(p)
        await db.flush()
        await db.refresh(p)
        assert p.kind == "operations" and p.result == {} and p.session_id is None
        assert p.expires_at is None and p.status == "draft"


async def test_one_open_builder_proposal_per_session(app_session: AppSessionFactory) -> None:
    tenant, session_id = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(IntegrityError):
        async with app_session(tenant) as db:
            db.add(_builder(tenant, session_id, "draft"))
            await db.flush()
            db.add(_builder(tenant, session_id, "awaiting_approval"))
            await db.flush()


async def test_a_superseded_one_does_not_count_as_open(app_session: AppSessionFactory) -> None:
    tenant, session_id = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        old, new = _builder(tenant, session_id, "superseded"), _builder(tenant, session_id, "draft")
        db.add(old)
        await db.flush()
        db.add(new)
        await db.flush()
        assert (old.status, new.status) == ("superseded", "draft")
        assert old.id != new.id


async def test_a_chat_session_has_no_mode_until_one_is_set(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        s = m.ChatSession(tenant_id=tenant, agent_id=uuid.uuid4(), member_id=uuid.uuid4())
        db.add(s)
        await db.flush()
        await db.refresh(s)
        assert s.mode is None
        s.mode = "agent"
        await db.flush()
        await db.refresh(s)
        assert s.mode == "agent"
