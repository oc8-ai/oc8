# backend/src/oc8/runtime/registry.py
"""Runtime resolution + fail-closed capability negotiation (§8.7).

Built-in runtimes live in a small closed map keyed by plugin name. A THIRD-PARTY
runtime supplies its own implementation through the plugin loader, exactly as
connectors do.

The tenant check is already done before we get there: ``load_runtime_plugin``
requires the plugin to be installed AND enabled for this tenant, so importing a
runtime module (which is process-global) never makes it usable by a tenant that
did not enable it. See ``oc8.capas.contributions`` for why that split matters.

See docs/superpowers/specs/2026-07-16-pluggable-runtimes-design.md."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit import append_event
from oc8.auth import Principal
from oc8.runtime.adapter import EchoRuntimeStub, Oc8AgentRuntime, RuntimeAdapter

RUNTIME_CAPABILITY_CHECKPOINTS = "checkpoints"
RUNTIME_CAPABILITY_SKILLS = "skills"

#: The two built-in runtimes, now independently selectable (not just a
#: tenant-wide `agent_isolation` toggle) -- see `GET /runtimes`
#: (api/v1/runtimes.py) and `resolve_runtime` below. Sentinel STRINGS, never
#: database uuids: there is no Capa row behind either one. Defined here (not
#: in api/v1/runtimes.py) so the picker (GET /runtimes), the assignment path
#: (assign_runtime), and resolution (resolve_runtime) share one definition of
#: what these two ids mean and cannot drift on it.
BUILTIN_IN_PROCESS_RUNTIME_REF = "builtin:in-process"
BUILTIN_ISOLATED_RUNTIME_REF = "builtin:isolated"
_BUILTIN_RUNTIME_REFS = {BUILTIN_IN_PROCESS_RUNTIME_REF, BUILTIN_ISOLATED_RUNTIME_REF}


class _LiveRow:
    """Sentinel type for "read `agent.runtime_ref` off the live row". Needed
    because `None` is itself a meaningful runtime_ref ("never set")."""


#: Default for `resolve_runtime`'s `runtime_ref`: no pinned value was given.
LIVE_ROW = _LiveRow()

# The two built-ins do NOT share a capability list: `maybe_checkpoint` is
# called only from `oc8.agent.engine` (the in-process path), while
# `DockerIsolatedRuntime` runs `oc8.isolated_shell` in a container -- a
# module that never reaches the engine. `plugins/nanoclaw_runtime/plugin.toml`
# omits `checkpoints` from its own manifest for exactly this reason. A
# built-in entry (or an assignment) claiming a capability the agent does not
# actually get would be a picker -- and a capability check -- that lies.
BUILTIN_IN_PROCESS_CAPABILITIES = [RUNTIME_CAPABILITY_CHECKPOINTS, RUNTIME_CAPABILITY_SKILLS]
BUILTIN_ISOLATED_CAPABILITIES = [RUNTIME_CAPABILITY_SKILLS]


def _isolated_runtime() -> type[RuntimeAdapter]:
    from oc8.runtime.isolated import DockerIsolatedRuntime

    return DockerIsolatedRuntime


_RUNTIME_IMPLEMENTATIONS: dict[str, type[RuntimeAdapter]] = {
    "oc8.agent-runtime": Oc8AgentRuntime,
    "oc8.echo-runtime-stub": EchoRuntimeStub,
    "oc8.agent-runtime-isolated": _isolated_runtime(),
}


class RuntimeResolutionError(RuntimeError):
    pass


class RuntimeNotFoundError(RuntimeError):
    """Raised by ``assign_runtime`` when the requested runtime reference does
    not exist -- not a known built-in sentinel, not a plugin that exists, is a
    runtime_adapter, and is enabled for this tenant, or not even shaped like a
    uuid."""

    def __init__(self, runtime_ref: str) -> None:
        super().__init__(f"runtime {runtime_ref!r} not found or not enabled")
        self.runtime_ref = runtime_ref


class RuntimeNotExecutableError(RuntimeError):
    """Raised by ``assign_runtime`` when the plugin has no loadable
    implementation."""

    def __init__(self, plugin_name: str) -> None:
        super().__init__(f"runtime {plugin_name!r} is not executable")
        self.plugin_name = plugin_name


class RuntimeCapabilityError(RuntimeError):
    """Raised by ``assign_runtime`` when the runtime lacks a capability the
    agent needs. Carries the violations so the caller can build the same
    422 body ``_violation_body`` has always produced."""

    def __init__(self, violations: list[RuntimeViolation]) -> None:
        super().__init__("runtime capability violation")
        self.violations = violations


@dataclass
class RuntimeViolation:
    kind: Literal["supervision", "skill"]
    missing_capability: str
    reason: str


def _contributed_runtime(plugin_name: str) -> type[RuntimeAdapter] | None:
    """Load the plugin's own implementation, if it ships one.

    Deferred imports: oc8.capas.loader reaches knowledge/connector code that
    imports back into the runtime package.
    """
    from oc8.capas.contributions import runtime_for
    from oc8.capas.discovery import find_plugin
    from oc8.capas.loader import load_plugin

    discovered = find_plugin(plugin_name)
    if discovered is None or not load_plugin(discovered):
        return None
    return runtime_for(plugin_name)


def is_runtime_executable(plugin_name: str) -> bool:
    """Whether an implementation exists at all. Says nothing about whether a
    given tenant may use it -- that is resolve_runtime's job."""
    if plugin_name in _RUNTIME_IMPLEMENTATIONS:
        return True
    return _contributed_runtime(plugin_name) is not None


async def load_runtime_plugin(
    db: AsyncSession, *, tenant_id: uuid.UUID, capa_id: uuid.UUID
) -> tuple[m.Capa, m.CapaVersion] | None:
    """Resolve a candidate runtime plugin id: must exist, be type
    runtime_adapter, have a current version, and be enabled for this tenant.
    RLS already scopes db.get(Capa, ...) to the current tenant; the
    explicit tenant_id filter below is on CapaInstallation, which checks
    a specific row's existence, not a plain by-PK lookup."""
    plugin = await db.get(m.Capa, capa_id)
    if plugin is None or plugin.type != "runtime_adapter" or plugin.current_version_id is None:
        return None
    version = await db.get(m.CapaVersion, plugin.current_version_id)
    if version is None:
        return None
    installation = (
        await db.execute(
            select(m.CapaInstallation).where(
                m.CapaInstallation.tenant_id == tenant_id,
                m.CapaInstallation.capa_id == capa_id,
                m.CapaInstallation.status == "enabled",
            )
        )
    ).scalar_one_or_none()
    if installation is None:
        return None
    return plugin, version


async def list_enabled_runtime_plugins_for_tenant(
    db: AsyncSession, *, tenant_id: uuid.UUID
) -> list[tuple[m.Capa, m.CapaVersion]]:
    """Every runtime_adapter plugin installed AND enabled for this tenant.

    Reuses ``load_runtime_plugin``'s tenant-scoped enabled-installation check
    per candidate rather than re-deriving it: the query that decides "may this
    tenant use this plugin" must stay a single definition, or the picker
    (this function) and the assignment path (``load_runtime_plugin``) can
    silently drift on what "enabled" means.
    """
    capa_ids = (
        (
            await db.execute(
                select(m.Capa.id).where(
                    m.Capa.type == "runtime_adapter", m.Capa.current_version_id.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    resolved = [
        await load_runtime_plugin(db, tenant_id=tenant_id, capa_id=capa_id) for capa_id in capa_ids
    ]
    return [r for r in resolved if r is not None]


async def resolve_runtime_plugin(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent: m.Agent,
    runtime_ref: _LiveRow | str | None = LIVE_ROW,
) -> tuple[m.Capa, m.CapaVersion] | None:
    """Resolve the agent's *currently assigned* runtime. None means "not a
    Capa-backed runtime" -- either never set, or explicitly pinned to one of
    the two built-in sentinels (BUILTIN_IN_PROCESS_RUNTIME_REF /
    BUILTIN_ISOLATED_RUNTIME_REF), neither of which has a Capa row. Callers
    that need to tell those two apart use `agent.runtime_ref` directly
    (resolve_runtime does, immediately below) -- this function only answers
    "is there a plugin behind this". A set-but-unresolvable plugin runtime_ref
    raises rather than returning None — a broken assignment must be
    surfaced everywhere it's encountered, not silently treated as unset.

    `runtime_ref` overrides the live row's value -- see `resolve_runtime`."""
    ref = agent.runtime_ref if isinstance(runtime_ref, _LiveRow) else runtime_ref
    if not ref or ref in _BUILTIN_RUNTIME_REFS:
        return None
    capa_id = uuid.UUID(ref)
    resolved = await load_runtime_plugin(db, tenant_id=tenant_id, capa_id=capa_id)
    if resolved is None:
        raise RuntimeResolutionError(f"runtime {ref!r} is not installed/enabled for this tenant")
    return resolved


async def resolve_runtime(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent: m.Agent,
    runtime_ref: _LiveRow | str | None = LIVE_ROW,
) -> RuntimeAdapter:
    """The runtime that executes `agent`'s run.

    A run passes its PINNED `runtime_ref` (`resolve_version(...)["runtime_ref"]`)
    so an unpublished runtime assignment never changes which runtime executes
    a run already in flight -- including a run re-executed on resume. `None`
    is a real value ("never set"), which is why "not given" is the separate
    `LIVE_ROW` sentinel; only a caller with no run behind it (the evidence
    sweep) reads the live row."""
    ref = agent.runtime_ref if isinstance(runtime_ref, _LiveRow) else runtime_ref
    # An explicit built-in choice wins outright, overriding the tenant-wide
    # agent_isolation setting below -- that's the whole point of making the
    # two built-ins independently selectable rather than one hidden toggle.
    if ref == BUILTIN_ISOLATED_RUNTIME_REF:
        from oc8.runtime.isolated import DockerIsolatedRuntime

        return DockerIsolatedRuntime()
    if ref == BUILTIN_IN_PROCESS_RUNTIME_REF:
        return Oc8AgentRuntime()
    # An explicit plugin runtime_ref wins next. Otherwise (runtime_ref never
    # set at all), when isolation is enabled for the deployment (§8.5), a
    # first_party agent runs in a per-agent container instead of in-process --
    # exactly today's behavior, untouched by the two branches above.
    resolved = await resolve_runtime_plugin(db, tenant_id=tenant_id, agent=agent, runtime_ref=ref)
    if resolved is None:
        from oc8.config import get_settings

        if get_settings().agent_isolation:
            from oc8.runtime.isolated import DockerIsolatedRuntime

            return DockerIsolatedRuntime()
        return Oc8AgentRuntime()
    plugin, _version = resolved
    impl = _RUNTIME_IMPLEMENTATIONS.get(plugin.name)
    if impl is None:
        impl = _contributed_runtime(plugin.name)
    if impl is None:
        # Not a silent fallback to the default runtime: the agent was pointed at
        # this plugin explicitly, so running something else would be wrong.
        raise RuntimeResolutionError(f"runtime {plugin.name!r} has no implementation")
    return impl()


def check_runtime_capabilities(
    *, has_supervision: bool, has_enabled_skills: bool, runtime_capabilities: list[str]
) -> list[RuntimeViolation]:
    violations: list[RuntimeViolation] = []
    if has_supervision and RUNTIME_CAPABILITY_CHECKPOINTS not in runtime_capabilities:
        violations.append(
            RuntimeViolation(
                kind="supervision",
                missing_capability=RUNTIME_CAPABILITY_CHECKPOINTS,
                reason="runtime does not declare 'checkpoints', required for supervised agents",
            )
        )
    if has_enabled_skills and RUNTIME_CAPABILITY_SKILLS not in runtime_capabilities:
        violations.append(
            RuntimeViolation(
                kind="skill",
                missing_capability=RUNTIME_CAPABILITY_SKILLS,
                reason=(
                    "runtime does not declare 'skills', required for agents with skill assignments"
                ),
            )
        )
    return violations


async def agent_has_supervision(db: AsyncSession, *, agent_id: uuid.UUID) -> bool:
    from oc8.runtime.supervision_hook import current_supervision_query_port

    return await current_supervision_query_port().has_supervision(db, agent_id=agent_id)


async def agent_has_enabled_skills(db: AsyncSession, *, agent_id: uuid.UUID) -> bool:
    result = await db.execute(
        select(m.SkillAssignment.id).where(
            m.SkillAssignment.agent_id == agent_id, m.SkillAssignment.enabled.is_(True)
        )
    )
    return result.first() is not None


async def assign_runtime(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent: m.Agent,
    runtime_ref: str | None,
    principal: Principal,
) -> None:
    """Validate and assign (or clear) an agent's runtime_ref.

    ``runtime_ref`` is a plain string, not a uuid: it names either a Capa's
    id (a plugin runtime) or one of the two built-in sentinels
    (BUILTIN_IN_PROCESS_RUNTIME_REF / BUILTIN_ISOLATED_RUNTIME_REF), which
    have no Capa row to look up.

    Shared by ``PUT /agents/{id}/runtime`` and ``POST /agents`` so the two
    routes cannot drift. Raises the same error conditions the PUT handler has
    always raised, now as typed exceptions the caller maps to its own HTTP
    responses:
      - runtime not found / not enabled for tenant  -> RuntimeNotFoundError
      - plugin has no loadable implementation        -> RuntimeNotExecutableError
      - runtime lacks a capability the agent needs    -> RuntimeCapabilityError

    Flushes but never commits -- the caller's endpoint owns the transaction
    and the RLS GUC is transaction-local (see module docstrings elsewhere in
    this codebase for why a mid-request commit is a bug, not a style choice).
    """
    if runtime_ref is None:
        agent.runtime_ref = None
        await db.flush()
        await append_event(
            db,
            tenant_id=tenant_id,
            actor_type="operator",
            actor_id=None,
            category="admin",
            action="agent.runtime.cleared",
            resource={"agent_id": str(agent.id), "by": principal.subject},
            principal=principal,
        )
        return

    if runtime_ref in _BUILTIN_RUNTIME_REFS:
        capabilities = (
            BUILTIN_IN_PROCESS_CAPABILITIES
            if runtime_ref == BUILTIN_IN_PROCESS_RUNTIME_REF
            else BUILTIN_ISOLATED_CAPABILITIES
        )
        has_supervision = await agent_has_supervision(db, agent_id=agent.id)
        has_enabled_skills = await agent_has_enabled_skills(db, agent_id=agent.id)
        violations = check_runtime_capabilities(
            has_supervision=has_supervision,
            has_enabled_skills=has_enabled_skills,
            runtime_capabilities=capabilities,
        )
        if violations:
            raise RuntimeCapabilityError(violations)

        agent.runtime_ref = runtime_ref
        await db.flush()
        await append_event(
            db,
            tenant_id=tenant_id,
            actor_type="operator",
            actor_id=None,
            category="admin",
            action="agent.runtime.assigned",
            resource={
                "agent_id": str(agent.id),
                "runtime_plugin_id": runtime_ref,
                "by": principal.subject,
            },
            principal=principal,
        )
        return

    try:
        capa_id = uuid.UUID(runtime_ref)
    except ValueError:
        raise RuntimeNotFoundError(runtime_ref) from None

    resolved = await load_runtime_plugin(db, tenant_id=tenant_id, capa_id=capa_id)
    if resolved is None:
        raise RuntimeNotFoundError(runtime_ref)
    plugin, version = resolved
    if not is_runtime_executable(plugin.name):
        raise RuntimeNotExecutableError(plugin.name)

    has_supervision = await agent_has_supervision(db, agent_id=agent.id)
    has_enabled_skills = await agent_has_enabled_skills(db, agent_id=agent.id)
    violations = check_runtime_capabilities(
        has_supervision=has_supervision,
        has_enabled_skills=has_enabled_skills,
        runtime_capabilities=list(version.capabilities),
    )
    if violations:
        raise RuntimeCapabilityError(violations)

    agent.runtime_ref = str(plugin.id)
    await db.flush()
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="agent.runtime.assigned",
        resource={
            "agent_id": str(agent.id),
            "runtime_plugin_id": str(plugin.id),
            "by": principal.subject,
        },
        principal=principal,
    )
