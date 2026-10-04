"""Plugin install/instantiate service (generalizes the agent-module service)."""

from __future__ import annotations

import hashlib
import json
import re
import uuid

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.agents.versioning import publish_version
from oc8.capas.discovery import DiscoveredPlugin, find_plugin
from oc8.capas.manifest import (
    Manifest,
    ManifestError,
    PluginSetupSpec,
    SetupFieldSpec,
    parse_manifest,
)
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
    "MissingDependencyError",
    "PluginError",
    "SetupValidationError",
    "install_plugin",
    "install_with_dependencies",
    "instantiate_agent",
    "instantiate_department",
    "validate_setup_values",
]


class PluginError(ValueError):
    pass


class DependencyError(PluginError):
    pass


class DuplicateVersionError(PluginError):
    pass


class CoreCompatError(PluginError):
    pass


class MissingDependencyError(PluginError):
    """A `plugin_depends` entry named a plugin that isn't on disk."""


class SetupValidationError(PluginError):
    """One of `configure_plugin`'s three request-shape checks failed."""


def validate_setup_values(
    setup: PluginSetupSpec, fields: dict[str, SetupFieldSpec], values: dict[str, str]
) -> None:
    """The three checks `configure_plugin` (api/v1/capas.py) runs against a
    submitted setup form before touching anything stateful: every submitted
    key is declared, every required field has a value, and any declared
    `any_of` credential-set alternative is satisfied by at least one group.
    Pulled out so the Copilot gateway's `capa.configure` operation (secret-
    blind, no MCP connection support) can run the identical validation the
    REST route does, instead of drifting from it.
    """
    unknown = sorted(set(values) - set(fields))
    if unknown:
        raise SetupValidationError(f"unknown setup fields: {', '.join(unknown)}")
    for field in setup.fields:
        if field.required and not values.get(field.key, field.default).strip():
            raise SetupValidationError(f"setup field {field.key!r} is required")
    for alternative in setup.validation.any_of:
        unknown_fields = sorted(set(alternative) - set(fields))
        if unknown_fields:
            raise SetupValidationError(
                f"setup validation references unknown fields: {', '.join(unknown_fields)}"
            )
    if setup.validation.any_of and not any(
        all(values.get(key, fields[key].default).strip() for key in alternative)
        for alternative in setup.validation.any_of
    ):
        alternatives = " or ".join(" + ".join(group) for group in setup.validation.any_of)
        raise SetupValidationError(f"provide one complete credential set: {alternatives}")


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


#: Hard ceiling on how many copies one template agent may become. A setup
#: form can offer a smaller range; it cannot hire past this.
MAX_TEMPLATE_REPEAT = 32


def _repeat_count(raw: str, key: str) -> int:
    text = raw.strip()
    if not text:
        return 1
    try:
        count = int(text)
    except ValueError as exc:
        raise PluginError(
            f"setup field {key!r} must be a whole number, got {raw!r}"
        ) from exc
    if count < 1 or count > MAX_TEMPLATE_REPEAT:
        raise PluginError(
            f"setup field {key!r} must be between 1 and {MAX_TEMPLATE_REPEAT}, got {count}"
        )
    return count


def _with_copy_index(value: object, index: int) -> object:
    token = str(index)
    if isinstance(value, str):
        return value.replace("{{n}}", token)
    if isinstance(value, dict):
        return {key: _with_copy_index(item, index) for key, item in value.items()}
    if isinstance(value, list):
        return [_with_copy_index(item, index) for item in value]
    return value


def _expand_agent_defs(
    agent_defs: list[dict[str, object]], config: dict[str, str]
) -> list[dict[str, object]]:
    """One template row becomes `repeat_from_setup` copies.

    A blank or missing setup value means one copy. The first copy keeps
    `is_team_lead`. Later copies report to that first copy when the template
    itself was the lead and named nobody else. `{{n}}` is the copy index.
    """
    expanded: list[dict[str, object]] = []
    for agent in agent_defs:
        key = str(agent.get("repeat_from_setup") or "").strip()
        count = _repeat_count(str(config.get(key, "") if key else ""), key) if key else 1
        first_name = ""
        for index in range(1, count + 1):
            copy = dict(_with_copy_index(agent, index))  # type: ignore[arg-type]
            template_name = str(agent.get("name") or "")
            if "{{n}}" in template_name:
                copy["name"] = template_name.replace("{{n}}", str(index))
            elif count > 1:
                copy["name"] = f"{template_name} {index}"
            else:
                copy["name"] = template_name
            if index == 1:
                first_name = str(copy["name"])
                copy["is_team_lead"] = bool(agent.get("is_team_lead", False))
            else:
                copy["is_team_lead"] = False
                if agent.get("is_team_lead") and not agent.get("reports_to"):
                    copy["reports_to"] = first_name
            expanded.append(copy)
    return expanded


async def _runtime_ref_for_template(
    db: AsyncSession, *, tenant_id: uuid.UUID, plugin_name: str
) -> str:
    """The installed, enabled runtime capa id a template agent named."""
    plugin = (
        await db.execute(
            select(Capa).where(Capa.tenant_id == tenant_id, Capa.name == plugin_name)
        )
    ).scalar_one_or_none()
    if plugin is None or plugin.type != "runtime_adapter":
        raise PluginError(f"runtime {plugin_name!r} is not an installed runtime")
    installation = (
        await db.execute(
            select(CapaInstallation).where(
                CapaInstallation.tenant_id == tenant_id,
                CapaInstallation.capa_id == plugin.id,
            )
        )
    ).scalar_one_or_none()
    if installation is None or installation.status != "enabled":
        raise PluginError(f"runtime {plugin_name!r} is not enabled")
    return str(plugin.id)


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


async def install_with_dependencies(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    found: DiscoveredPlugin,
    origin: str,
    _seen: set[str] | None = None,
) -> CapaVersion | None:
    """Install `found` for `tenant_id`, first recursively installing any
    not-yet-installed `plugin_depends` entries (design §9). Never enables a
    dependency -- only `install_plugin` runs for it, exactly as if an
    operator had installed it manually and not yet clicked Enable.

    Moved here from `api/v1/capas.py` (was `_install_with_dependencies`) so
    both the REST route and the Copilot gateway's `capa.install` operation
    can call it without a service module importing a route module. Domain
    exceptions (`PluginError`/`MissingDependencyError`) replace the two
    `HTTPException` raises the route-local version used -- callers map them
    to their own transport's error shape (`HTTPException` for the REST
    route, `InvalidOperation` for the Copilot gateway).
    """
    plugin_id = found.plugin_id
    seen = _seen if _seen is not None else set()
    if plugin_id in seen:
        return None
    seen.add(plugin_id)

    if not found.valid or found.manifest is None:
        raise PluginError(found.error or "invalid manifest")

    for dep_name in found.manifest.get("plugin_depends") or []:
        already_installed = (
            await db.execute(select(Capa).where(Capa.tenant_id == tenant_id, Capa.name == dep_name))
        ).scalar_one_or_none()
        if already_installed is None:
            dep = find_plugin(dep_name)
            if dep is None:
                msg = f"{plugin_id} depends on a plugin not found on disk: {dep_name}"
                raise MissingDependencyError(msg)
            await install_with_dependencies(
                db, tenant_id=tenant_id, found=dep, origin=origin, _seen=seen
            )

    try:
        return await install_plugin(
            db, tenant_id=tenant_id, manifest_data=found.manifest, origin=origin
        )
    except DuplicateVersionError:
        raise


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
        # `presentation` is the DISPLAY side of an agent (agent_to_dto reads it);
        # `definition` is the behavioural side. A prompt starter is a UI
        # affordance, so it belongs here.
        presentation={"prompt_starters": list(spec.get("prompt_starters") or [])},
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
    # v1 in the same transaction as the hire, after the skill assignments, so
    # the snapshot includes them -- same create-and-publish shape as
    # api/v1/agents_write.py::create_agent. Every run is pinned to a version;
    # an agent without one would run off its live row.
    await publish_version(db, agent)
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
    agent_defs = _expand_agent_defs(list(spec.get("agents", [])), config)

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
            presentation={"prompt_starters": list(a.get("prompt_starters") or [])},
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
        runtime_name = str(a.get("runtime") or "").strip()
        if runtime_name:
            agent.runtime_ref = await _runtime_ref_for_template(
                db, tenant_id=tenant_id, plugin_name=runtime_name
            )
        # Per agent, after its skills and runtime -- see instantiate_agent.
        await publish_version(db, agent)
        if lead_id is None and agent.is_team_lead:
            lead_id = agent.id

    if lead_id is not None:
        dept.team_lead_agent_id = lead_id
    await db.flush()
    return dept
