from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_a_new_member_starts_pending(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T", settings={}))
        await db.flush()
        member = m.OrgMember(tenant_id=tenant, subject="new@acme.test", subject_uuid=uuid.uuid4())
        db.add(member)
        await db.flush()
        await db.refresh(member)
        assert member.onboarding_status == "pending"
