from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.service import install_plugin
from oc8.constants import ACME_TENANT_ID
from oc8.supervision.service import RuntimeCapabilityError, set_supervisor
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _install_stub_runtime(
    db: AsyncSession, tenant: uuid.UUID, *, capabilities: list[str]
) -> uuid.UUID:
    # Unique semver suffix per call — ACME_TENANT_ID is shared across the
    # whole suite and this helper is called by multiple tests in this file;
    # see the identical note in tests/runtime/test_registry.py.
    version = await install_plugin(
        db,
        tenant_id=tenant,
        manifest_data={
            "name": "oc8.echo-runtime-stub",
            "version": f"1.0.0+{uuid.uuid4().hex[:8]}",
            "type": "runtime_adapter",
            "trust": "community",
            "capabilities": capabilities,
        },
    )
    await enable_plugin(db, tenant_id=tenant, capa_id=version.capa_id, granted_permissions=[])
    return version.capa_id


async def test_set_supervisor_succeeds_for_default_runtime(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        dept_id = uuid.uuid4()
        supervisor = m.Agent(tenant_id=tenant, department_id=dept_id, name="Sup")
        supervised = m.Agent(tenant_id=tenant, department_id=dept_id, name="Sub")
        db.add_all([supervisor, supervised])
        await db.flush()

        assignment = await set_supervisor(
            db,
            tenant_id=tenant,
            supervised_agent_id=supervised.id,
            supervisor_agent_id=supervisor.id,
            department_id=dept_id,
        )
        assert assignment.supervised_agent_id == supervised.id


async def test_set_supervisor_rejects_runtime_without_checkpoints(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        plugin_id = await _install_stub_runtime(db, tenant, capabilities=["streaming"])
        dept_id = uuid.uuid4()
        supervisor = m.Agent(tenant_id=tenant, department_id=dept_id, name="Sup")
        supervised = m.Agent(
            tenant_id=tenant, department_id=dept_id, name="Sub", runtime_ref=str(plugin_id)
        )
        db.add_all([supervisor, supervised])
        await db.flush()

        with pytest.raises(RuntimeCapabilityError):
            await set_supervisor(
                db,
                tenant_id=tenant,
                supervised_agent_id=supervised.id,
                supervisor_agent_id=supervisor.id,
                department_id=dept_id,
            )


async def test_set_supervisor_succeeds_for_runtime_with_checkpoints(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        plugin_id = await _install_stub_runtime(db, tenant, capabilities=["checkpoints"])
        dept_id = uuid.uuid4()
        supervisor = m.Agent(tenant_id=tenant, department_id=dept_id, name="Sup")
        supervised = m.Agent(
            tenant_id=tenant, department_id=dept_id, name="Sub", runtime_ref=str(plugin_id)
        )
        db.add_all([supervisor, supervised])
        await db.flush()

        assignment = await set_supervisor(
            db,
            tenant_id=tenant,
            supervised_agent_id=supervised.id,
            supervisor_agent_id=supervisor.id,
            department_id=dept_id,
        )
        assert assignment.supervised_agent_id == supervised.id
