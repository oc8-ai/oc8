"""Turn a plugin's manifest data into domain rows (§13).

`skill`, `flow_template` and `tool_pack` plugins carry no code -- they are data
that has to become a Skill, a Flow, or MCP connections before anything can use
them. `agent_template` capas may also ship `skills/` (merged into `skill_pack`
by discovery); those skills materialise here on enable the same way.
`department_template` has done agents since B2 via an explicit
`instantiate_department`; skill/flow/tool_pack materialise on **enable** instead,
because they have nothing to name or place and enable is where consent already
happens.

Three rules hold across all of them:

* **Idempotent.** Re-enabling (after a permission change, say) must not create a
  second copy.
* **Per tenant.** Rows are written for the enabling tenant only.
* **Non-destructive.** Disabling a plugin does NOT delete what it created.
  Toggling a plugin off must not silently destroy work built on top of it;
  removal is a separate, deliberate act.

A malformed data block is logged and skipped rather than raised: the enable
itself is what the operator asked for, and leaving them with a half-enabled
plugin because one guardrail string was wrong helps nobody.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.capas.manifest import Manifest, SkillTemplateSpec, ToolPackConnection
from oc8.skills.schema import SkillDefinitionError, parse_definition

logger = logging.getLogger(__name__)


def _hash(payload: dict[str, Any]) -> bytes:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _slugify(value: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "item"


async def _materialise_skill(db: AsyncSession, *, tenant_id: uuid.UUID, manifest: Manifest) -> None:
    """Every skill the plugin ships -- one, or a whole set.

    Iterated rather than singular because a standard set for a company is one
    decision, not one per skill. Each is created independently, so a set that
    grows in a later version delivers only what is new.
    """
    specs = list(manifest.skill_pack.skills) if manifest.skill_pack else []
    if manifest.skill_template is not None:
        specs.append(manifest.skill_template)
    for spec in specs:
        await _materialise_one_skill(db, tenant_id=tenant_id, manifest=manifest, spec=spec)


async def _materialise_one_skill(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    manifest: Manifest,
    spec: SkillTemplateSpec,
) -> None:
    existing = (
        await db.execute(
            select(m.Skill).where(m.Skill.tenant_id == tenant_id, m.Skill.name == spec.name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return

    definition: dict[str, Any] = {
        "schema_version": 1,
        "slug": _slugify(spec.name),
        "version": manifest.version,
        "instruction": spec.instruction.strip(),
        "requires": {"tools": list(spec.requires_tools), "kbs": list(spec.requires_kbs)},
        "guardrails": list(spec.guardrails),
        "presentation": {
            "tools": list(spec.requires_tools),
            "knowledge": list(spec.requires_kbs),
            "guardrails": list(spec.guardrails),
        },
        # capa_name/skill_subpath, self-sufficient for agent/control_tools.py's
        # read_reference_file to resolve at runtime via
        # capas.discovery.find_plugin(capa_name).path -- None when this skill
        # has no on-disk directory to serve from (spec.reference_root's own
        # docstring explains the three cases).
        "reference_root": (
            f"{manifest.name}/{spec.reference_root}".rstrip("/")
            if spec.reference_root is not None
            else None
        ),
    }
    try:
        parse_definition(definition)
    except SkillDefinitionError as exc:
        logger.warning("plugin %s ships an unusable skill, skipped: %s", manifest.name, exc)
        return

    skill = m.Skill(
        tenant_id=tenant_id,
        name=spec.name,
        category=spec.category or None,
        description=spec.description,
        author=manifest.name,
        origin="store",
        trust_level=manifest.trust,
    )
    db.add(skill)
    await db.flush()
    version = m.SkillVersion(
        tenant_id=tenant_id,
        skill_id=skill.id,
        semver=manifest.version,
        definition=definition,
        artifact_hash=_hash(definition),
    )
    db.add(version)
    await db.flush()
    skill.current_version_id = version.id
    await db.flush()


async def _materialise_flow(db: AsyncSession, *, tenant_id: uuid.UUID, manifest: Manifest) -> None:
    spec = manifest.flow_template
    if spec is None:
        return
    existing = (
        await db.execute(
            select(m.Flow).where(m.Flow.tenant_id == tenant_id, m.Flow.name == spec.name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return

    flow = m.Flow(tenant_id=tenant_id, name=spec.name)
    db.add(flow)
    await db.flush()
    version = m.FlowVersion(
        tenant_id=tenant_id,
        flow_id=flow.id,
        semver=manifest.version,
        spec=dict(spec.spec),
        artifact_hash=_hash(spec.spec),
    )
    db.add(version)
    await db.flush()
    flow.current_version_id = version.id
    await db.flush()


#: Parts of a connection's config that are DECLARED by the manifest and never
#: chosen by an operator: the neutral seams -- how a call's value and its record
#: are read, and which of its tools reach a person. Everything else -- the
#: command, the URL, the database, the credentials -- is the operator's, and a
#: plugin upgrade must not touch it.
DECLARED_SEAMS = ("focus_spec", "value_spec", "outward_tools", "outward_skip_spec", "tool_notes")


def _refresh_declared_seams(existing: m.McpConnection, conn: ToolPackConnection) -> None:
    """Carry a manifest's own declarations onto a connection already installed.

    Without this a connection is frozen at the version that created it: adding
    an entity to `focus_spec` fixes nothing for the tenant that already has the
    plugin, and the fix appears to do nothing at all. That is not a hypothetical
    -- a missing label is exactly why a live log stayed blank for a whole run.
    """
    config = dict(existing.config or {})
    declared = dict(conn.config)
    changed = False
    for key in DECLARED_SEAMS:
        if key in declared and config.get(key) != declared[key]:
            config[key] = declared[key]
            changed = True
    if changed:
        existing.config = config
    scopes = conn.scopes if isinstance(conn.scopes, dict) else list(conn.scopes)
    if existing.scopes != scopes:
        existing.scopes = scopes


async def _materialise_tool_pack(
    db: AsyncSession, *, tenant_id: uuid.UUID, manifest: Manifest
) -> None:
    spec = manifest.tool_pack
    if spec is None:
        return
    for conn in spec.connections:
        # MANY, not one: the setup form creates a connection per department, so
        # the same plugin legitimately has several rows under one name. Reading
        # this as one-or-none crashed the whole enable as soon as a second
        # department was set up -- and every one of them needs the refresh.
        existing = (
            (
                await db.execute(
                    select(m.McpConnection).where(
                        m.McpConnection.tenant_id == tenant_id,
                        m.McpConnection.name == conn.name,
                    )
                )
            )
            .scalars()
            .all()
        )
        if existing:
            for row in existing:
                _refresh_declared_seams(row, conn)
            continue
        db.add(unbound_connection(tenant_id=tenant_id, manifest=manifest, conn=conn))
    await db.flush()


def unbound_connection(
    *, tenant_id: uuid.UUID, manifest: Manifest, conn: ToolPackConnection
) -> m.McpConnection:
    """The department-less connection a tool pack's manifest describes, before
    any setup form gave it a department or an identity."""
    config = dict(conn.config)
    config["_plugin_name"] = manifest.name
    config["_connection_key"] = conn.key
    return m.McpConnection(
        tenant_id=tenant_id,
        name=conn.name,
        server_url=conn.server_url,
        transport=conn.transport,
        scopes=conn.scopes if isinstance(conn.scopes, dict) else list(conn.scopes),
        config=config,
        # Created UNCONNECTED and unhealthy-unknown on purpose: a manifest may
        # describe a server, but only an operator running the connection test
        # may declare it reachable.
        connected=False,
        health={},
    )


async def materialise_plugin_data(
    db: AsyncSession, *, tenant_id: uuid.UUID, version: m.CapaVersion
) -> None:
    """Write the domain rows a data-only plugin describes. Add/flush only --
    the caller owns the commit. Never raises for bad plugin data."""
    try:
        manifest = Manifest.model_validate(version.manifest)
    except Exception:
        logger.exception("plugin version %s has an unreadable manifest", version.id)
        return
    await _materialise_skill(db, tenant_id=tenant_id, manifest=manifest)
    await _materialise_flow(db, tenant_id=tenant_id, manifest=manifest)
    await _materialise_tool_pack(db, tenant_id=tenant_id, manifest=manifest)
