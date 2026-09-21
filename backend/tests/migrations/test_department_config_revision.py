"""department gets the same trigger-maintained config_revision column
migration 0054 gave agent/plugin/integration — Copilot's staleness check
for department.update/delete/restore reads this column."""

from __future__ import annotations

import pytest

from oc8 import models as m

pytestmark = pytest.mark.asyncio


async def test_department_config_revision_starts_at_one_and_bumps_on_update(
    app_session, acme_tenant
) -> None:
    async with app_session(acme_tenant) as db:
        dept = m.Department(tenant_id=acme_tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        assert dept.config_revision == 1

        dept.name = "Operations"
        await db.flush()
        await db.refresh(dept)
        assert dept.config_revision == 2
