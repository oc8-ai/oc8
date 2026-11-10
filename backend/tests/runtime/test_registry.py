# backend/tests/runtime/test_registry.py
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.service import install_plugin
from oc8.config import get_settings
from oc8.constants import ACME_TENANT_ID
from oc8.runtime.adapter import EchoRuntimeStub, Oc8AgentRuntime
from oc8.runtime.isolated import DockerIsolatedRuntime
from oc8.runtime.registry import (
    BUILTIN_IN_PROCESS_RUNTIME_REF,
    BUILTIN_ISOLATED_RUNTIME_REF,
    RuntimeResolutionError,
    agent_has_enabled_skills,
    agent_has_supervision,
    check_runtime_capabilities,
    is_runtime_executable,
    load_runtime_plugin,
    resolve_runtime,
    resolve_runtime_plugin,
)
from oc8.runtime.supervision_hook import NOOP_SUPERVISION_RUN_HOOK, use_supervision_runtime
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def test_no_requirements_always_compatible() -> None:
    assert (
        check_runtime_capabilities(
            has_supervision=False, has_enabled_skills=False, runtime_capabilities=[]
        )
        == []
    )


def test_supervision_without_checkpoints_violates() -> None:
    violations = check_runtime_capabilities(
        has_supervision=True, has_enabled_skills=False, runtime_capabilities=[]
    )
    assert len(violations) == 1
    assert violations[0].kind == "supervision"
    assert violations[0].missing_capability == "checkpoints"


def test_skills_without_skills_capability_violates() -> None:
    violations = check_runtime_capabilities(
        has_supervision=False, has_enabled_skills=True, runtime_capabilities=[]
    )
    assert len(violations) == 1
    assert violations[0].kind == "skill"
    assert violations[0].missing_capability == "skills"


def test_both_missing_produces_two_violations() -> None:
    violations = check_runtime_capabilities(
        has_supervision=True, has_enabled_skills=True, runtime_capabilities=[]
    )
    assert len(violations) == 2


def test_compatible_when_capabilities_present() -> None:
    violations = check_runtime_capabilities(
        has_supervision=True,
        has_enabled_skills=True,
        runtime_capabilities=["checkpoints", "skills"],
    )
    assert violations == []


def test_is_runtime_executable() -> None:
    assert is_runtime_executable("oc8.agent-runtime") is True
    assert is_runtime_executable("oc8.echo-runtime-stub") is True
    assert is_runtime_executable("community.unknown-runtime") is False


async def _install_stub_runtime(
    db: AsyncSession,
    tenant: uuid.UUID,
    *,
    name: str = "oc8.echo-runtime-stub",
    capabilities: list[str] | None = None,
) -> uuid.UUID:
    # `install_plugin` reuses an existing Plugin row by (tenant, name) but
    # raises DuplicateVersionError on a repeated (plugin_id, semver) — and
    # ACME_TENANT_ID is shared across every test in the suite. A unique
    # semver per call avoids colliding with any other test that also
    # installs a plugin named "oc8.echo-runtime-stub" (this codebase has
    # hit this exact class of cross-test collision twice already this
    # session — see .superpowers/sdd/progress.md history for §10/§13).
    version_tag = f"1.0.0+{uuid.uuid4().hex[:8]}"
    version = await install_plugin(
        db,
        tenant_id=tenant,
        manifest_data={
            "name": name,
            "version": version_tag,
            "type": "runtime_adapter",
            "trust": "community",
            "capabilities": capabilities if capabilities is not None else ["streaming"],
        },
    )
    await enable_plugin(db, tenant_id=tenant, capa_id=version.capa_id, granted_permissions=[])
    return version.capa_id


async def test_resolve_runtime_defaults_to_first_party(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()

        assert await resolve_runtime_plugin(db, tenant_id=tenant, agent=agent) is None
        runtime = await resolve_runtime(db, tenant_id=tenant, agent=agent)
        assert isinstance(runtime, DockerIsolatedRuntime)


async def test_resolve_runtime_returns_stub_when_assigned(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        plugin_id = await _install_stub_runtime(db, tenant)
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="A",
            runtime_ref=str(plugin_id),
        )
        db.add(agent)
        await db.flush()

        resolved = await resolve_runtime_plugin(db, tenant_id=tenant, agent=agent)
        assert resolved is not None
        plugin, version = resolved
        assert plugin.id == plugin_id
        assert version.capabilities == ["streaming"]

        runtime = await resolve_runtime(db, tenant_id=tenant, agent=agent)
        assert isinstance(runtime, EchoRuntimeStub)


async def test_resolve_runtime_unset_ref_still_follows_agent_isolation_setting(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one invariant the built-in-sentinel feature must not break: an
    agent whose `runtime_ref` was NEVER touched resolves via the tenant-wide
    `agent_isolation` setting exactly as before -- zero behavior change for
    the ordinary case."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="A")
        db.add(agent)
        await db.flush()
        assert agent.runtime_ref is None

        monkeypatch.setattr(get_settings(), "agent_isolation", False, raising=False)
        assert isinstance(await resolve_runtime(db, tenant_id=tenant, agent=agent), Oc8AgentRuntime)

        monkeypatch.setattr(get_settings(), "agent_isolation", True, raising=False)
        assert isinstance(
            await resolve_runtime(db, tenant_id=tenant, agent=agent), DockerIsolatedRuntime
        )


async def test_resolve_runtime_explicit_builtin_sentinel_overrides_agent_isolation_setting(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent explicitly pinned to one of the two built-ins keeps it
    regardless of what the tenant-wide `agent_isolation` setting says later --
    that override is the entire point of making the two built-ins
    independently selectable instead of one hidden toggle."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        isolated_agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Isolated-pinned",
            runtime_ref=BUILTIN_ISOLATED_RUNTIME_REF,
        )
        in_process_agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="In-process-pinned",
            runtime_ref=BUILTIN_IN_PROCESS_RUNTIME_REF,
        )
        db.add_all([isolated_agent, in_process_agent])
        await db.flush()

        # Tenant-wide setting says "in-process" -- the isolated-pinned agent
        # must still get isolated, and vice versa below.
        monkeypatch.setattr(get_settings(), "agent_isolation", False, raising=False)
        assert isinstance(
            await resolve_runtime(db, tenant_id=tenant, agent=isolated_agent), DockerIsolatedRuntime
        )
        assert isinstance(
            await resolve_runtime(db, tenant_id=tenant, agent=in_process_agent), Oc8AgentRuntime
        )

        # Flip the tenant-wide setting the other way -- both pinned agents
        # must stay exactly where they were explicitly pointed.
        monkeypatch.setattr(get_settings(), "agent_isolation", True, raising=False)
        assert isinstance(
            await resolve_runtime(db, tenant_id=tenant, agent=isolated_agent), DockerIsolatedRuntime
        )
        assert isinstance(
            await resolve_runtime(db, tenant_id=tenant, agent=in_process_agent), Oc8AgentRuntime
        )

        # Neither sentinel is Capa-backed, so resolve_runtime_plugin must
        # treat them the same as "unset" -- None, not a raised error.
        assert await resolve_runtime_plugin(db, tenant_id=tenant, agent=isolated_agent) is None
        assert await resolve_runtime_plugin(db, tenant_id=tenant, agent=in_process_agent) is None


async def test_resolve_runtime_raises_on_nonexistent_ref(app_session: AppSessionFactory) -> None:
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

        with pytest.raises(RuntimeResolutionError):
            await resolve_runtime_plugin(db, tenant_id=tenant, agent=agent)
        with pytest.raises(RuntimeResolutionError):
            await resolve_runtime(db, tenant_id=tenant, agent=agent)


async def test_resolve_runtime_raises_on_disabled_plugin(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        version = await install_plugin(
            db,
            tenant_id=tenant,
            manifest_data={
                "name": "oc8.echo-runtime-stub-disabled",
                "version": "1.0.0",
                "type": "runtime_adapter",
                "trust": "community",
                "capabilities": [],
            },
        )
        # Never enabled.
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="A",
            runtime_ref=str(version.capa_id),
        )
        db.add(agent)
        await db.flush()

        with pytest.raises(RuntimeResolutionError):
            await resolve_runtime(db, tenant_id=tenant, agent=agent)


async def test_resolve_runtime_raises_on_no_implementation(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        plugin_id = await _install_stub_runtime(
            db, tenant, name="community.unregistered-runtime", capabilities=["checkpoints"]
        )
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="A",
            runtime_ref=str(plugin_id),
        )
        db.add(agent)
        await db.flush()

        # Resolves the plugin fine (it's a valid, enabled runtime_adapter)...
        resolved = await resolve_runtime_plugin(db, tenant_id=tenant, agent=agent)
        assert resolved is not None
        # ...but there's no Python implementation registered for its name.
        with pytest.raises(RuntimeResolutionError):
            await resolve_runtime(db, tenant_id=tenant, agent=agent)


async def test_load_runtime_plugin_rejects_wrong_type(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        version = await install_plugin(
            db,
            tenant_id=tenant,
            manifest_data={
                "name": "acme.some-skill",
                "version": "1.0.0",
                "type": "skill",
                "trust": "first_party",
            },
        )
        await enable_plugin(db, tenant_id=tenant, capa_id=version.capa_id, granted_permissions=[])
        assert await load_runtime_plugin(db, tenant_id=tenant, capa_id=version.capa_id) is None


class _AssignedSupervisionQueryPort:
    def __init__(self, assigned_agent_id: uuid.UUID) -> None:
        self._assigned_agent_id = assigned_agent_id

    async def has_supervision(self, db: AsyncSession, *, agent_id: uuid.UUID) -> bool:
        return agent_id == self._assigned_agent_id


async def test_agent_capability_queries_use_the_edition_port(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        supervised = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Sub")
        db.add(supervised)
        await db.flush()

        assert await agent_has_supervision(db, agent_id=supervised.id) is False
        assert await agent_has_enabled_skills(db, agent_id=supervised.id) is False

        with use_supervision_runtime(
            NOOP_SUPERVISION_RUN_HOOK,
            _AssignedSupervisionQueryPort(supervised.id),
        ):
            assert await agent_has_supervision(db, agent_id=supervised.id) is True


async def test_the_tenant_assistant_runs_in_process_unless_pinned(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Assistant's model calls and tools run control-plane side anyway; a
    sandbox would only add a pod per turn. With no runtime of its own it runs
    in-process whatever `agent_isolation` says; an explicit choice still wins,
    and every other agent keeps the tenant-wide fallback."""
    tenant = uuid.uuid4()
    monkeypatch.setattr(get_settings(), "agent_isolation", True, raising=False)
    async with app_session(tenant) as db:
        assistant = m.Agent(
            tenant_id=tenant, department_id=uuid.uuid4(), name="Assistant", is_tenant_assistant=True
        )
        other = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Paula")
        db.add_all([assistant, other])
        await db.flush()

        assert isinstance(
            await resolve_runtime(db, tenant_id=tenant, agent=assistant), Oc8AgentRuntime
        )
        # A run passes its PINNED ref; None is "never set".
        assert isinstance(
            await resolve_runtime(db, tenant_id=tenant, agent=assistant, runtime_ref=None),
            Oc8AgentRuntime,
        )
        assert isinstance(
            await resolve_runtime(
                db, tenant_id=tenant, agent=assistant, runtime_ref=BUILTIN_ISOLATED_RUNTIME_REF
            ),
            DockerIsolatedRuntime,
        )
        assert isinstance(
            await resolve_runtime(db, tenant_id=tenant, agent=other), DockerIsolatedRuntime
        )
