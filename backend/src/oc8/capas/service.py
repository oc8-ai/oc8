"""Plugin install/instantiate service (generalizes the agent-module service)."""

from __future__ import annotations

import re
import hashlib
import json
import uuid

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.capas.manifest import Manifest, ManifestError, parse_manifest
from oc8.capas.registry import enabled_capability_registry
from oc8.constants import CORE_VERSION
from oc8.models import (
    Agent,
    Capa,
    CapaInstallation,
    CapaVersion,
    Department,
    MemoryStore,
    Skill,
    SkillAssignment,
)
from oc8.triggers.service import InvalidTriggerConfig
from oc8.triggers.service import create_trigger as create_trigger_row

__all__ = [
    "CoreCompatError",
    "DependencyError",
    "DuplicateVersionError",
    "ManifestError",
    "PluginError",
    "install_plugin",
    "instantiate_agent",
    "instantiate_department",
]


class PluginError(ValueError):
    pass


class DependencyError(PluginError):
    pass


class DuplicateVersionError(PluginError):
    pass


class CoreCompatError(PluginError):
    pass


def _artifact_hash(manifest: Manifest) -> bytes:
    canonical = json.dumps(manifest.model_dump(mode="json"), sort_keys=True).encode()
    return hashlib.sha256(canonical).digest()


async def _load_installation_config(
    db: AsyncSession, *, capa_id: uuid.UUID
) -> dict[str, str]:
    """The tenant-submitted, non-secret setup values for this capa, if any
    setup was ever run -- the same dict `_configure_without_connection`
    (api/v1/capas.py) writes to. `{}` both when no CapaInstallation row
    exists yet (every pre-existing hand-built test fixture, and any capa
    hired before ever being enabled+configured) and when one exists with an
    empty config -- both mean "no substitution values available", handled
    identically by `_substitute_template_values` leaving every token as-is."""
    installation = (
        await db.execute(
            select(CapaInstallation).where(CapaInstallation.capa_id == capa_id)
        )
    ).scalar_one_or_none()
    return dict(installation.config) if installation is not None else {}


def _check_core_compat(spec: str) -> None:
    if not spec:
        return
    try:
        if Version(CORE_VERSION) not in SpecifierSet(spec):
            raise CoreCompatError(f"core {CORE_VERSION} does not satisfy core_compat {spec!r}")
    except InvalidSpecifier as exc:
        raise CoreCompatError(f"invalid core_compat {spec!r}: {exc}") from exc


def _substitute_template_values(text: str, config: dict[str, str]) -> str:
    """Replace {{key}} tokens in `text` with config[key]. A token with no
    matching key is left as-is -- hiring with no setup run yet (every
    pre-existing template) must keep behaving exactly as it does today."""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        return config.get(key, match.group(0))

    return re.sub(r"\{\{(\w+)\}\}", _sub, text)


async def install_plugin(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    manifest_data: dict[str, object],
    author: str = "",
    origin: str = "local",
    trust_level: str | None = None,
) -> CapaVersion:
    manifest = parse_manifest(manifest_data)
    _check_core_compat(manifest.core_compat)
    # Asked of the database, per install: what is enabled right now is the only
    # thing that can satisfy a dependency, and it outlives this process.
    missing = (await enabled_capability_registry(db)).missing(manifest.depends)
    if missing:
        raise DependencyError(f"unmet capabilities: {missing}")

    effective_trust = trust_level or manifest.trust
    plugin = (
        await db.execute(
            select(Capa).where(Capa.tenant_id == tenant_id, Capa.name == manifest.name)
        )
    ).scalar_one_or_none()
    if plugin is None:
        plugin = Capa(
            tenant_id=tenant_id,
            name=manifest.name,
            type=manifest.type,
            author=author,
            origin=origin,
            trust_level=effective_trust,
            core_compat=manifest.core_compat,
        )
        db.add(plugin)
        await db.flush()

    dup = (
        await db.execute(
            select(CapaVersion).where(
                CapaVersion.capa_id == plugin.id,
                CapaVersion.semver == manifest.version,
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise DuplicateVersionError(f"{manifest.name}@{manifest.version} already installed")

    version = CapaVersion(
        tenant_id=tenant_id,
        capa_id=plugin.id,
        semver=manifest.version,
        manifest=manifest.model_dump(mode="json"),
        artifact_hash=_artifact_hash(manifest),
        permissions=list(manifest.permissions),
        capabilities=list(manifest.capabilities),
        entry_points=dict(manifest.entry_points),
    )
    db.add(version)
    await db.flush()
    plugin.current_version_id = version.id
    await db.flush()
    return version


async def _assign_named_skills(
    db: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, skill_names: list[str]
) -> None:
    """Turn an agent template's `skills` name list into real `SkillAssignment`
    rows -- the read side of the runtime's own skill resolution
    (`skills/runtime.py`), which only ever looks at `SkillAssignment`, never at
    `Agent.definition["skills"]`. A name with no matching LOCAL Skill row in
    this tenant yet (its sibling `skill` capa may not be installed) is skipped,
    not raised -- same "malformed/incomplete data does not block the rest of
    the install" convention `materialise.py` already uses.

    `Skill.name` has no unique constraint on `(tenant_id, name)` -- an
    archived skill sharing a name with a live one is a real (if unlikely)
    shape, and `scalar_one_or_none()` raises `MultipleResultsFound` on it,
    which is not a `PluginError` and would 500 the whole install. Note this
    intentionally does NOT filter by `origin`: a department/agent template's
    sibling `skill` capa materialises its Skill row with `origin="store"`
    (materialise.py), not "local" -- the export side's "only local skills
    are exportable" rule is a property of the SOURCE tenant's data, not a
    constraint on what this install-side lookup may bind to in the TARGET
    tenant. Filtering to live rows only and taking the first deterministically
    (oldest wins) avoids the crash without narrowing which row it can find."""
    for name in skill_names:
        skill = (
            (
                await db.execute(
                    select(Skill)
                    .where(
                        Skill.tenant_id == tenant_id,
                        Skill.name == name,
                        Skill.deleted_at.is_(None),
                    )
                    .order_by(Skill.created_at)
                )
            )
            .scalars()
            .first()
        )
        if skill is None or skill.current_version_id is None:
            continue
        db.add(
            SkillAssignment(
                tenant_id=tenant_id,
                agent_id=agent_id,
                skill_version_id=skill.current_version_id,
            )
        )
    await db.flush()


async def _create_trigger_if_present(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    trigger: dict[str, object] | None,
    config: dict[str, str],
) -> None:
    """Goes through `triggers/service.py::create_trigger` -- the single funnel
    every trigger creation is required to go through (see that function's own
    docstring) -- rather than constructing a `Trigger` row directly. A
    hand-built row never got `next_run_at` set, and the scheduler only ever
    selects `next_run_at <= now` (triggers/scheduler.py); a NULL there can
    never satisfy that comparison, so every trigger installed from a capa
    template was silently, permanently dead. Routing through `create_trigger`
    also gets cron-expression validation (a hand-edited manifest's
    `cron_expression` is an unvalidated `str` at the `TemplateAgent` schema
    level) and startup jitter for free.

    `config` substitutes {{field_key}} tokens into `task_text`/
    `cron_expression` before validation -- see `_substitute_template_values`."""
    if not trigger:
        return
    try:
        await create_trigger_row(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            kind="cron",
            task_text=_substitute_template_values(str(trigger.get("task_text", "")), config),
            cron_expression=_substitute_template_values(
                str(trigger.get("cron_expression", "")), config
            ),
        )
    except InvalidTriggerConfig as exc:
        raise PluginError(f"invalid trigger in template: {exc}") from exc


async def instantiate_agent(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    version: CapaVersion,
    department_id: uuid.UUID,
    name: str | None = None,
) -> Agent:
    mf = version.manifest
    if mf.get("type", "agent_template") != "agent_template":
        raise PluginError("only agent_template plugins can be instantiated as agents")
    config = await _load_installation_config(db, capa_id=version.capa_id)
    # Same shape as one entry under department_template.agents — when present,
    # mission/persona land on the Agent row. Absent = legacy thin instantiate.
    spec = dict(mf.get("agent_template") or {})
    definition: dict[str, object] = {
        "plugin": mf.get("name", ""),
        "version": version.semver,
        "persona": _substitute_template_values(str(spec.get("persona", "")), config),
        "skills": list(spec.get("skills") or []),
    }
    if spec.get("max_steps"):
        definition["max_steps"] = int(spec["max_steps"])
    agent = Agent(
        tenant_id=tenant_id,
        department_id=department_id,
        name=name or str(spec.get("name") or mf.get("name", "Agent")),
        role_title=_substitute_template_values(str(spec.get("role_title") or ""), config),
        mission=_substitute_template_values(str(spec.get("mission") or ""), config),
        status="stopped",
        narrowing=dict(spec.get("narrowing") or {}),
        definition=definition,
    )
    db.add(agent)
    await db.flush()
    db.add(MemoryStore(tenant_id=tenant_id, tier="agent", owner_id=agent.id))
    await db.flush()
    await _assign_named_skills(
        db, tenant_id=tenant_id, agent_id=agent.id, skill_names=list(spec.get("skills") or [])
    )
    await _create_trigger_if_present(
        db,
        tenant_id=tenant_id,
        agent_id=agent.id,
        trigger=spec.get("trigger"),
        config=config,
    )
    return agent


async def instantiate_department(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    version: CapaVersion,
    name: str | None = None,
) -> Department:
    """Create a Department + its team of stopped Agents from a department_template
    plugin version. add/flush only -- the endpoint owns the commit."""
    mf = version.manifest
    if mf.get("type") != "department_template" or not mf.get("department_template"):
        raise PluginError("only department_template plugins can be instantiated as departments")
    config = await _load_installation_config(db, capa_id=version.capa_id)
    spec = mf["department_template"]
    agent_defs = list(spec.get("agents", []))

    names = [a["name"] for a in agent_defs]
    if len(names) != len(set(names)):
        raise PluginError("template agent names must be unique")
    nameset = set(names)
    for a in agent_defs:
        rt = a.get("reports_to")
        if rt is not None and rt not in nameset:
            raise PluginError(f"reports_to references an unknown agent: {rt!r}")

    dept = Department(
        tenant_id=tenant_id,
        name=name or mf.get("name", "Department"),
        frame=dict(spec.get("frame", {})),
        # Captured once, here, and never refreshed by a later template
        # upgrade -- see the column's own docstring (models/core.py) for why.
        frame_capa_defaults=dict(spec.get("frame", {})),
    )
    db.add(dept)
    await db.flush()

    lead_id: uuid.UUID | None = None
    for a in agent_defs:
        agent = Agent(
            tenant_id=tenant_id,
            department_id=dept.id,
            name=a["name"],
            role_title=_substitute_template_values(str(a.get("role_title", "")), config),
            mission=_substitute_template_values(str(a.get("mission", "")), config),
            is_team_lead=bool(a.get("is_team_lead", False)),
            status="stopped",
            narrowing=dict(a.get("narrowing", {})),
            definition={
                "plugin": mf.get("name", ""),
                "version": version.semver,
                "persona": _substitute_template_values(str(a.get("persona", "")), config),
                "reports_to": a.get("reports_to"),
                "skills": list(a.get("skills", [])),
                **({"max_steps": int(a["max_steps"])} if a.get("max_steps") else {}),
            },
        )
        db.add(agent)
        await db.flush()
        db.add(MemoryStore(tenant_id=tenant_id, tier="agent", owner_id=agent.id))
        await _assign_named_skills(
            db, tenant_id=tenant_id, agent_id=agent.id, skill_names=list(a.get("skills") or [])
        )
        await _create_trigger_if_present(
            db,
            tenant_id=tenant_id,
            agent_id=agent.id,
            trigger=a.get("trigger"),
            config=config,
        )
        if lead_id is None and agent.is_team_lead:
            lead_id = agent.id

    if lead_id is not None:
        dept.team_lead_agent_id = lead_id
    await db.flush()
    return dept
