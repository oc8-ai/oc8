"""The query port reads supervision assignments from the table."""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.constants import ACME_TENANT_ID
from oc8.supervision.runtime import SupervisionQueryPortImpl
from oc8.supervision.service import ensure_default_policy, set_supervisor
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_query_port_reads_an_existing_assignment(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        department_id = uuid.uuid4()
        supervisor = m.Agent(tenant_id=tenant, department_id=department_id, name="Sup")
        supervised = m.Agent(tenant_id=tenant, department_id=department_id, name="Sub")
        db.add_all([supervisor, supervised])
        await db.flush()

        port = SupervisionQueryPortImpl()
        assert await port.has_supervision(db, agent_id=supervised.id) is False

        await ensure_default_policy(db, tenant_id=tenant, department_id=department_id)
        await set_supervisor(
            db,
            tenant_id=tenant,
            supervised_agent_id=supervised.id,
            supervisor_agent_id=supervisor.id,
            department_id=department_id,
        )

        assert await port.has_supervision(db, agent_id=supervised.id) is True
