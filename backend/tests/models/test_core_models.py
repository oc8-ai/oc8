from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_organization_timezone_defaults_to_utc(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        org = m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T", settings={})
        db.add(org)
        await db.flush()
        await db.refresh(org)
        assert org.timezone == "UTC"


async def test_organization_timezone_is_settable(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        org = m.Organization(
            id=tenant, slug=f"t-{tenant.hex[:8]}", name="T", settings={}, timezone="Europe/Berlin"
        )
        db.add(org)
        await db.flush()
        await db.refresh(org)
        assert org.timezone == "Europe/Berlin"
