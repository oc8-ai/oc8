"""Inverse of `capas/service.py` + `capas/materialise.py` (Capa-Exporter
design, docs/superpowers/specs/2026-09-02-capa-exporter-design.md): existing
Department/Agent/Skill DB rows -> a `Manifest` this same install pipeline can
read back unmodified. Every `build_*_export` constructs a real `Manifest`
(the same pydantic model `capas/manifest.py` already defines for install) and
renders it with `tomli_w` -- an export `parse_manifest` cannot re-parse is a
serializer bug, proven directly by the round-trip test in
`tests/release/test_capa_export_round_trip.py` (Task 4)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

import tomli_w
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.capas.manifest import (
    DepartmentTemplateSpec,
    Manifest,
    SkillTemplateSpec,
    TemplateAgent,
    TemplateAgentTrigger,
    parse_manifest,
)

__all__ = [
    "ExportValidationError",
    "ExportedCapa",
    "build_agent_export",
    "build_department_export",
    "build_skill_export",
    "build_tool_pack_export",
]

#: The same folder-name convention `discovery.py` enforces (folder name must
#: equal manifest `name`) -- checked here so the wizard's Details step can
#: reject a bad name before any DB work happens.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Raw McpConnection ids a tool-policy dict may carry -- `connection_id` at
#: agent-narrowing level (`ToolPolicy.to_json()`, authz/pdp.py) and
#: `default_connection_id` at department-frame level (`ToolPolicyWriteDTO`,
#: api/v1/departments.py). Both name a connection specific to THIS tenant
#: (agent tool login selection design); neither is portable, so both are
#: stripped from every policy dict this module ever exports, regardless of
#: which level it came from -- a department's cascade can copy its own
#: `default_connection_id` verbatim onto an agent's narrowing
#: (departments.py's `set_department_tools`), so checking only the "native"
#: key for each level is not enough.
_NON_PORTABLE_POLICY_KEYS = ("connection_id", "default_connection_id")


class ExportValidationError(ValueError):
    """A selection-level problem the wizard's Preview step must show
    inline -- never a silently dropped item."""


@dataclass
class ExportedCapa:
    folder_name: str
    manifest_toml: str
    warnings: list[str] = field(default_factory=list)
    #: Relative path (e.g. "skills/crm_follow_up.toml") -> file content,
    #: written alongside plugin.toml in the ZIP. Empty for agent/department
    #: exports, which have no sibling files of their own.
    extra_files: dict[str, str] = field(default_factory=dict)


def _check_capa_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise ExportValidationError(
            f"capa name {name!r} must match {_NAME_RE.pattern} (lowercase, "
            "starts with a letter, only letters/digits/underscore)"
        )


async def _resolve_tool_grants(
    db: AsyncSession, *, tenant_id: uuid.UUID, tools: dict[str, object]
) -> tuple[dict[str, object], list[str], list[str]]:
    """`tools` keys by McpConnection NAME (departments.py:569 confirms this
    convention). For each key: resolve its connection to the capa that
    created it (`McpConnection.config["_plugin_name"]`, written by
    `materialise.py`'s `_materialise_tool_pack`, materialise.py:214-216),
    then that capa's current capabilities. A key with no resolvable owner is
    dropped with a warning rather than shipped as a dangling reference.

    Returns (kept_tools, depends, warnings).
    """
    kept: dict[str, object] = {}
    depends: set[str] = set()
    warnings: list[str] = []
    for key, policy in tools.items():
        conn = (
            (
                await db.execute(
                    select(m.McpConnection).where(
                        m.McpConnection.tenant_id == tenant_id, m.McpConnection.name == key
                    )
                )
            )
            .scalars()
            .first()
        )
        plugin_name = (conn.config or {}).get("_plugin_name") if conn else None
        capa = (
            (
                await db.execute(
                    select(m.Capa).where(m.Capa.tenant_id == tenant_id, m.Capa.name == plugin_name)
                )
            ).scalar_one_or_none()
            if plugin_name
            else None
        )
        version = (
            await db.get(m.CapaVersion, capa.current_version_id)
            if capa and capa.current_version_id
            else None
        )
        if version is None or not version.capabilities:
            warnings.append(
                f"tool grant '{key}' has no resolvable capability -- dropped from the export; "
                "the target environment would have no way to satisfy it"
            )
            continue
        kept[key] = _sanitize_policy(key, policy, warnings)
        depends.update(version.capabilities)
    return kept, sorted(depends), warnings


def _sanitize_policy(key: str, policy: object, warnings: list[str]) -> object:
    """Strip any raw connection id out of one tool's policy dict before it
    can reach a manifest -- a connection id is a row in THIS tenant's own
    `mcp_connection` table, meaningless (and a leak) in another tenant. The
    rest of the policy (rights, approval settings, `only`) is portable and
    kept as-is.

    Also drops any key whose value is `None` (e.g. an unset `approval_eur`
    or `only`, per `ToolPolicy.to_json()`, authz/pdp.py:107-109) -- TOML has
    no null literal, so a raw `None` surviving into the manifest crashes
    `tomli_w.dumps` (`frame`/`narrowing` are untyped `dict[str, Any]` in
    manifest.py, so pydantic's `exclude_none` on the outer `Manifest` never
    reaches into them). `ToolPolicy.from_json()` reads every field via
    `data.get(...)`, so a missing key and an explicit `null` are the same
    value on install -- dropping the key changes nothing but the render."""
    if not isinstance(policy, dict):
        return policy
    dropped = [k for k in _NON_PORTABLE_POLICY_KEYS if policy.get(k)]
    if dropped:
        warnings.append(
            f"tool grant '{key}' pins a specific connection login ({', '.join(dropped)}) -- "
            "that pin is tenant-specific and was dropped from the export; the target tenant "
            "must choose its own connection for this tool after install"
        )
    return {
        k: v
        for k, v in policy.items()
        if k not in _NON_PORTABLE_POLICY_KEYS and v is not None
    }


async def _local_skill_names_for_agent(
    db: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> list[str]:
    """Names of LOCAL skills this agent has via `SkillAssignment` (agent-scoped
    rows only -- department/tenant-wide assignments are a live runtime
    resolution, not something this one agent's own template should claim)."""
    rows = (
        (
            await db.execute(
                select(m.Skill.name)
                .select_from(m.SkillAssignment)
                .join(m.SkillVersion, m.SkillVersion.id == m.SkillAssignment.skill_version_id)
                .join(m.Skill, m.Skill.id == m.SkillVersion.skill_id)
                .where(
                    m.SkillAssignment.tenant_id == tenant_id,
                    m.SkillAssignment.agent_id == agent_id,
                    m.SkillAssignment.enabled.is_(True),
                    m.Skill.origin == "local",
                )
            )
        )
        .scalars()
        .all()
    )
    return sorted(set(rows))


def _agent_trigger(triggers: list[m.Trigger]) -> TemplateAgentTrigger | None:
    for t in triggers:
        if t.kind == "cron" and t.enabled and t.cron_expression:
            return TemplateAgentTrigger(cron_expression=t.cron_expression, task_text=t.task_text)
    return None


def _agent_narrowing_tools(agent: m.Agent) -> dict[str, object]:
    """Only the keys an OPERATOR explicitly overrode
    (`narrowing_overridden_keys`, models/core.py:266) -- not every key the
    department-defaults cascade wrote onto this agent's `narrowing`. The
    department's own `frame.tools` already carries the shared defaults;
    exporting the full cascaded dict would duplicate them at the agent level
    and make every agent's template look like it overrides everything."""
    overridden = set(agent.narrowing_overridden_keys or [])
    if not overridden:
        return {}
    tools = (agent.narrowing or {}).get("tools") or {}
    return {k: v for k, v in tools.items() if k in overridden}


def _render(manifest: Manifest) -> str:
    payload = {"plugin": manifest.model_dump(mode="json", exclude_none=True, exclude_defaults=True)}
    return tomli_w.dumps(payload)


async def build_skill_export(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    skill_id: uuid.UUID,
    capa_name: str,
    version: str,
    summary: str,
) -> ExportedCapa:
    _check_capa_name(capa_name)
    skill = await db.get(m.Skill, skill_id)
    if skill is None or skill.tenant_id != tenant_id:
        raise ExportValidationError(f"skill {skill_id} not found")
    if skill.origin != "local":
        raise ExportValidationError(
            f"skill {skill.name!r} is not local (origin={skill.origin!r}) -- "
            "only locally authored skills can be exported"
        )
    if skill.current_version_id is None:
        raise ExportValidationError(f"skill {skill.name!r} has no current version")
    sv = await db.get(m.SkillVersion, skill.current_version_id)
    assert sv is not None
    definition = sv.definition or {}
    spec = SkillTemplateSpec(
        name=skill.name,
        description=skill.description,
        category=skill.category or "",
        instruction=str(definition.get("instruction", "")),
        requires_tools=list((definition.get("requires") or {}).get("tools") or []),
        requires_kbs=[],  # Non-Goals: no KB portability
        guardrails=list(definition.get("guardrails") or []),
    )
    manifest = Manifest(name=capa_name, version=version, type="skill", summary=summary)
    # A skill's own body lives in a sibling skills/<name>.toml, NOT inline in
    # plugin.toml -- `discovery.py::_read_oc8_table` hard-rejects an inline
    # skill_template table (August 2026 plugin package restructure). That
    # file's top level IS the skill's fields directly, so it is rendered as
    # its own standalone document rather than wrapped in {"plugin": ...}.
    skill_toml = tomli_w.dumps(
        spec.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    )
    return ExportedCapa(
        folder_name=capa_name,
        manifest_toml=_render(manifest),
        warnings=[],
        extra_files={f"skills/{capa_name}.toml": skill_toml},
    )


async def build_agent_export(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    capa_name: str,
    version: str,
    summary: str,
) -> ExportedCapa:
    _check_capa_name(capa_name)
    agent = await db.get(m.Agent, agent_id)
    if agent is None or agent.tenant_id != tenant_id:
        raise ExportValidationError(f"agent {agent_id} not found")

    warnings: list[str] = []
    reports_to = (agent.definition or {}).get("reports_to")
    if reports_to:
        warnings.append(
            f"agent {agent.name!r} reports_to {reports_to!r} in its live definition, but a "
            "standalone agent_template has no department to resolve that against -- dropped"
        )
        reports_to = None

    # A standalone agent export includes every local skill it has directly --
    # there is no sibling selection to restrict the name list against, unlike
    # the department case below.
    local_skill_names = await _local_skill_names_for_agent(
        db, tenant_id=tenant_id, agent_id=agent.id
    )

    kept_tools, depends, tool_warnings = await _resolve_tool_grants(
        db, tenant_id=tenant_id, tools=_agent_narrowing_tools(agent)
    )
    warnings.extend(tool_warnings)
    narrowing = {"tools": kept_tools} if kept_tools else {}

    triggers = (
        (await db.execute(select(m.Trigger).where(m.Trigger.agent_id == agent.id)))
        .scalars()
        .all()
    )

    template = TemplateAgent(
        name=agent.name,
        role_title=agent.role_title,
        mission=agent.mission,
        is_team_lead=False,
        reports_to=reports_to,
        skills=local_skill_names,
        persona=str((agent.definition or {}).get("persona", "")),
        narrowing=narrowing,
        max_steps=int((agent.definition or {}).get("max_steps") or 0),
        trigger=_agent_trigger(list(triggers)),
    )
    manifest = Manifest(
        name=capa_name,
        version=version,
        type="agent_template",
        summary=summary,
        agent_template=template,
        depends=depends,
    )
    return ExportedCapa(folder_name=capa_name, manifest_toml=_render(manifest), warnings=warnings)


async def build_department_export(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    department_id: uuid.UUID,
    capa_name: str,
    version: str,
    summary: str,
) -> ExportedCapa:
    _check_capa_name(capa_name)
    dept = await db.get(m.Department, department_id)
    if dept is None or dept.tenant_id != tenant_id:
        raise ExportValidationError(f"department {department_id} not found")

    agents = (
        (
            await db.execute(
                select(m.Agent).where(
                    m.Agent.tenant_id == tenant_id, m.Agent.department_id == dept.id
                )
            )
        )
        .scalars()
        .all()
    )
    names = [a.name for a in agents]
    if len(names) != len(set(names)):
        raise ExportValidationError("template agent names must be unique")

    warnings: list[str] = []
    frame_tools, depends, tool_warnings = await _resolve_tool_grants(
        db, tenant_id=tenant_id, tools=dict((dept.frame or {}).get("tools") or {})
    )
    warnings.extend(tool_warnings)

    lead_name = next((a.name for a in agents if a.id == dept.team_lead_agent_id), None)

    # Skills reachable from THIS export: every local skill any agent here has
    # via SkillAssignment. Restricting each agent's own list to this set is
    # what keeps a name the target has no way to resolve out of the manifest
    # (design's Component 1) -- a skill only reachable via a DIFFERENT
    # department's agent is not part of this export and must not leak in.
    agent_ids = [a.id for a in agents]
    all_assignments = (
        await db.execute(
            select(m.SkillAssignment.agent_id, m.Skill.name)
            .join(m.SkillVersion, m.SkillVersion.id == m.SkillAssignment.skill_version_id)
            .join(m.Skill, m.Skill.id == m.SkillVersion.skill_id)
            .where(
                m.SkillAssignment.tenant_id == tenant_id,
                m.SkillAssignment.agent_id.in_(agent_ids),
                m.SkillAssignment.enabled.is_(True),
                m.Skill.origin == "local",
            )
        )
    ).all()
    skills_by_agent: dict[uuid.UUID, list[str]] = {}
    for agent_pk, skill_name in all_assignments:
        skills_by_agent.setdefault(agent_pk, []).append(skill_name)

    triggers_by_agent: dict[uuid.UUID, list[m.Trigger]] = {}
    all_triggers = (
        (await db.execute(select(m.Trigger).where(m.Trigger.agent_id.in_(agent_ids))))
        .scalars()
        .all()
    )
    for t in all_triggers:
        triggers_by_agent.setdefault(t.agent_id, []).append(t)

    templates: list[TemplateAgent] = []
    for agent in agents:
        is_lead = agent.id == dept.team_lead_agent_id
        reports_to = None if is_lead else lead_name
        narrowing: dict[str, object] = {}
        agent_tools = _agent_narrowing_tools(agent)
        if agent_tools:
            kept, agent_depends, agent_tool_warnings = await _resolve_tool_grants(
                db, tenant_id=tenant_id, tools=agent_tools
            )
            warnings.extend(agent_tool_warnings)
            depends.extend(d for d in agent_depends if d not in depends)
            if kept:
                narrowing = {"tools": kept}
        templates.append(
            TemplateAgent(
                name=agent.name,
                role_title=agent.role_title,
                mission=agent.mission,
                is_team_lead=is_lead,
                reports_to=reports_to,
                skills=sorted(skills_by_agent.get(agent.id, [])),
                persona=str((agent.definition or {}).get("persona", "")),
                narrowing=narrowing,
                max_steps=int((agent.definition or {}).get("max_steps") or 0),
                trigger=_agent_trigger(triggers_by_agent.get(agent.id, [])),
            )
        )

    dept_spec = DepartmentTemplateSpec(
        frame={"tools": frame_tools, "memory": dict((dept.frame or {}).get("memory") or {})},
        agents=templates,
    )
    manifest = Manifest(
        name=capa_name,
        version=version,
        type="department_template",
        summary=summary,
        department_template=dept_spec,
        depends=sorted(set(depends)),
    )
    return ExportedCapa(folder_name=capa_name, manifest_toml=_render(manifest), warnings=warnings)


async def build_tool_pack_export(
    db: AsyncSession, *, tenant_id: uuid.UUID, capa_id: uuid.UUID
) -> ExportedCapa:
    """A tool-pack capa's export is its OWN current manifest, re-validated
    and re-rendered -- unlike agent/department/skill exports, there is no
    live entity to reconstruct a manifest FROM: `install_plugin` already
    stored one (`CapaVersion.manifest`), and this is its only portable
    representation. `capa_name`/`version`/`summary` are therefore not
    parameters here (contrast the other three `build_*_export` functions):
    the wizard cannot rename a tool-pack capa through export, only through a
    fresh wizard run under a new name (spec §2's repeat-name-is-new-version
    path)."""
    capa = await db.get(m.Capa, capa_id)
    if capa is None or capa.tenant_id != tenant_id:
        raise ExportValidationError(f"capa {capa_id} not found")
    if capa.type != "tool_pack":
        raise ExportValidationError(f"capa {capa.name!r} is not a tool pack (type={capa.type!r})")
    if capa.current_version_id is None:
        raise ExportValidationError(f"capa {capa.name!r} has no current version")
    version = await db.get(m.CapaVersion, capa.current_version_id)
    assert version is not None
    manifest = parse_manifest(version.manifest)
    warnings: list[str] = []
    if manifest.tool_pack is not None:
        for conn in manifest.tool_pack.connections:
            cfg = dict(conn.config)
            dropped = [k for k in _NON_PORTABLE_POLICY_KEYS if cfg.get(k)]
            if dropped:
                warnings.append(
                    f"connection '{conn.key}' carried tenant-specific reference(s) "
                    f"({', '.join(dropped)}) -- dropped from the export"
                )
                for key in _NON_PORTABLE_POLICY_KEYS:
                    cfg.pop(key, None)
            conn.config = cfg
    return ExportedCapa(folder_name=capa.name, manifest_toml=_render(manifest), warnings=warnings)
