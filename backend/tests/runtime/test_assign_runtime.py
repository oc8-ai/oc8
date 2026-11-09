# backend/tests/runtime/test_assign_runtime.py
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit.chain import flush_audit
from oc8.auth import Principal
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.service import install_plugin
from oc8.constants import ACME_TENANT_ID
from oc8.runtime.registry import (
    BUILTIN_IN_PROCESS_RUNTIME_REF,
    BUILTIN_ISOLATED_RUNTIME_REF,
    RuntimeCapabilityError,
    RuntimeNotExecutableError,
    RuntimeNotFoundError,
    assign_runtime,
)
from oc8.runtime.supervision_hook import NOOP_SUPERVISION_RUN_HOOK, use_supervision_runtime
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _principal(tenant: uuid.UUID) -> Principal:
    return Principal(subject="op-1", tenant_id=tenant, role="org_admin")


async def _install_stub_runtime(
    db: AsyncSession,
    tenant: uuid.UUID,
    *,
    name: str = "oc8.echo-runtime-stub",
    capabilities: list[str] | None = None,
) -> uuid.UUID:
    # Unique semver suffix per call -- ACME_TENANT_ID is shared across the
    # whole suite; see the identical note in tests/runtime/test_registry.py.
    version = await install_plugin(
        db,
        tenant_id=tenant,
        manifest_data={
            "name": name,
            "version": f"1.0.0+{uuid.uuid4().hex[:8]}",
            "type": "runtime_adapter",
            "trust": "community",
            "capabilities": capabilities if capabilities is not None else [],
        },
    )
    await enable_plugin(db, tenant_id=tenant, capa_id=version.capa_id, granted_permissions=[])
    return version.capa_id


async def test_assign_runtime_sets_ref_and_audits(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        capa_id = await _install_stub_runtime(db, tenant)
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        await assign_runtime(
            db,
            tenant_id=tenant,
            agent=agent,
            runtime_ref=str(capa_id),
            principal=_principal(tenant),
        )

        assert agent.runtime_ref == str(capa_id)

        await flush_audit(db)  # staged audit events reach the chain at commit
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "agent.runtime.assigned")
                )
            )
            .scalars()
            .all()
        )
        assert any(
            e.resource.get("agent_id") == str(agent.id)
            and e.resource.get("runtime_plugin_id") == str(capa_id)
            for e in events
        )


async def test_assign_runtime_none_clears_ref_and_audits(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="A",
            runtime_ref=str(uuid.uuid4()),
        )
        db.add(agent)
        await db.flush()

        await assign_runtime(
            db,
            tenant_id=tenant,
            agent=agent,
            runtime_ref=None,
            principal=_principal(tenant),
        )

        assert agent.runtime_ref is None

        await flush_audit(db)  # staged audit events reach the chain at commit
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "agent.runtime.cleared")
                )
            )
            .scalars()
            .all()
        )
        assert any(e.resource.get("agent_id") == str(agent.id) for e in events)


async def test_assign_runtime_raises_not_found_for_unknown_plugin(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        with pytest.raises(RuntimeNotFoundError):
            await assign_runtime(
                db,
                tenant_id=tenant,
                agent=agent,
                runtime_ref=str(uuid.uuid4()),
                principal=_principal(tenant),
            )
        assert agent.runtime_ref is None


async def test_assign_runtime_raises_not_executable(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        capa_id = await _install_stub_runtime(db, tenant, name="community.unregistered-runtime")
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        with pytest.raises(RuntimeNotExecutableError):
            await assign_runtime(
                db,
                tenant_id=tenant,
                agent=agent,
                runtime_ref=str(capa_id),
                principal=_principal(tenant),
            )
        assert agent.runtime_ref is None


async def test_assign_runtime_raises_capability_error_with_violations(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        capa_id = await _install_stub_runtime(db, tenant, capabilities=[])
        skill_capable_capa_id = capa_id
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        skill = m.Skill(tenant_id=tenant, name="do-thing", category="general")
        db.add(skill)
        await db.flush()
        skill_version = m.SkillVersion(
            tenant_id=tenant,
            skill_id=skill.id,
            semver="1.0.0",
            definition={"requires": {}},
            artifact_hash=b"test",
        )
        db.add(skill_version)
        await db.flush()
        assignment = m.SkillAssignment(
            tenant_id=tenant,
            agent_id=agent.id,
            skill_version_id=skill_version.id,
            enabled=True,
        )
        db.add(assignment)
        await db.flush()

        with pytest.raises(RuntimeCapabilityError) as exc_info:
            await assign_runtime(
                db,
                tenant_id=tenant,
                agent=agent,
                runtime_ref=str(skill_capable_capa_id),
                principal=_principal(tenant),
            )
        assert any(v.kind == "skill" for v in exc_info.value.violations)
        assert agent.runtime_ref is None


class _AssignedSupervisionQueryPort:
    def __init__(self, assigned_agent_id: uuid.UUID) -> None:
        self._assigned_agent_id = assigned_agent_id

    async def has_supervision(self, db: AsyncSession, *, agent_id: uuid.UUID) -> bool:
        return agent_id == self._assigned_agent_id


async def test_assign_runtime_accepts_builtin_sentinels_and_audits(
    app_session: AppSessionFactory,
) -> None:
    """Neither built-in sentinel has a Capa row -- assign_runtime must set
    `runtime_ref` and audit exactly like a real plugin id does, without going
    anywhere near `load_runtime_plugin`."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        await assign_runtime(
            db,
            tenant_id=tenant,
            agent=agent,
            runtime_ref=BUILTIN_ISOLATED_RUNTIME_REF,
            principal=_principal(tenant),
        )
        assert agent.runtime_ref == BUILTIN_ISOLATED_RUNTIME_REF

        await assign_runtime(
            db,
            tenant_id=tenant,
            agent=agent,
            runtime_ref=BUILTIN_IN_PROCESS_RUNTIME_REF,
            principal=_principal(tenant),
        )
        assert agent.runtime_ref == BUILTIN_IN_PROCESS_RUNTIME_REF

        await flush_audit(db)  # staged audit events reach the chain at commit
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "agent.runtime.assigned")
                )
            )
            .scalars()
            .all()
        )
        assert any(
            e.resource.get("agent_id") == str(agent.id)
            and e.resource.get("runtime_plugin_id") == BUILTIN_ISOLATED_RUNTIME_REF
            for e in events
        )
        assert any(
            e.resource.get("agent_id") == str(agent.id)
            and e.resource.get("runtime_plugin_id") == BUILTIN_IN_PROCESS_RUNTIME_REF
            for e in events
        )


async def test_assign_runtime_rejects_isolated_builtin_for_a_supervised_agent(
    app_session: AppSessionFactory,
) -> None:
    """BUILTIN_ISOLATED_CAPABILITIES has no "checkpoints" -- assigning the
    isolated built-in to a supervised agent must raise the same
    RuntimeCapabilityError a plugin lacking that capability would, not skip
    the check just because there's no Capa row to look up."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Supervised")
        db.add(agent)
        await db.flush()
        agent_id = agent.id

        with use_supervision_runtime(
            NOOP_SUPERVISION_RUN_HOOK,
            _AssignedSupervisionQueryPort(agent_id),
        ):
            with pytest.raises(RuntimeCapabilityError) as exc_info:
                await assign_runtime(
                    db,
                    tenant_id=tenant,
                    agent=agent,
                    runtime_ref=BUILTIN_ISOLATED_RUNTIME_REF,
                    principal=_principal(tenant),
                )
            assert any(v.kind == "supervision" for v in exc_info.value.violations)
            assert agent.runtime_ref is None

            # The in-process built-in DOES declare "checkpoints" -- same
            # supervised agent, same call, must succeed.
            await assign_runtime(
                db,
                tenant_id=tenant,
                agent=agent,
                runtime_ref=BUILTIN_IN_PROCESS_RUNTIME_REF,
                principal=_principal(tenant),
            )
            assert agent.runtime_ref == BUILTIN_IN_PROCESS_RUNTIME_REF


async def test_assign_runtime_rejects_a_garbage_runtime_ref(
    app_session: AppSessionFactory,
) -> None:
    """Neither a known sentinel nor a parseable uuid -- must fail closed with
    the same RuntimeNotFoundError an unknown plugin id raises, not an
    unhandled ValueError from the uuid.UUID() parse."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        with pytest.raises(RuntimeNotFoundError):
            await assign_runtime(
                db,
                tenant_id=tenant,
                agent=agent,
                runtime_ref="not-a-uuid-or-a-sentinel",
                principal=_principal(tenant),
            )
        assert agent.runtime_ref is None
