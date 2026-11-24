"""Load an agent's enabled skills and shape them for the agent loop (§6.5).

Mirrors the retriever shape of `memory.router.retrieve_context` (§10) and
`knowledge.retrieval.retrieve_kb_context` (§11) so the engine reads
consistently: an async loader returning data, plus pure formatters that return
"" when there is nothing to say.

Degrades rather than raises. A skill whose definition cannot be parsed, or whose
version row has gone, is skipped with a warning -- it must not fail a run that
is otherwise fine.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.harness.procedures import checklist
from oc8.modelrouter import NeutralTool
from oc8.skills.schema import SkillDefinition, SkillDefinitionError, parse_definition

logger = logging.getLogger(__name__)

SKILL_TOOL_PREFIX = "skill_"


@dataclass(frozen=True)
class LoadedSkill:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    name: str
    description: str
    tool_name: str
    definition: SkillDefinition
    creator_id: uuid.UUID | None


def _slugify(value: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return out or "skill"


async def _held_rows(
    db: AsyncSession,
    rows: Sequence[tuple[m.SkillAssignment, m.SkillVersion | None, m.Skill | None]],
    entries: dict[str, list[Any]],
    tenant_id: uuid.UUID,
) -> list[tuple[m.SkillAssignment, m.SkillVersion | None, m.Skill | None]]:
    """`rows` as the owner confirmed them (`held_skill_entries`): an
    assignment they did not confirm is left out, one whose version moved
    since runs the confirmed version. `SkillVersion` rows never change, so
    the confirmed one, read by id, is exactly what they confirmed."""
    kept = [(a, v, s) for a, v, s in rows if str(a.id) in entries]
    wanted = {uuid.UUID(entries[str(a.id)][0]) for a, _v, _s in kept}
    moved = wanted - {a.skill_version_id for a, _v, _s in kept}
    confirmed: dict[uuid.UUID, tuple[m.SkillVersion, m.Skill | None]] = {}
    if moved:
        found = await db.execute(
            select(m.SkillVersion, m.Skill)
            .outerjoin(m.Skill, m.Skill.id == m.SkillVersion.skill_id)
            .where(m.SkillVersion.tenant_id == tenant_id, m.SkillVersion.id.in_(moved))
        )
        confirmed = {version.id: (version, skill) for version, skill in found.tuples()}
    out: list[tuple[m.SkillAssignment, m.SkillVersion | None, m.Skill | None]] = []
    for assignment, version, skill in kept:
        version_id = uuid.UUID(entries[str(assignment.id)][0])
        if version_id != assignment.skill_version_id:
            version, skill = confirmed.get(version_id, (None, None))
        out.append((assignment, version, skill))
    return out


async def load_assigned_skills(
    db: AsyncSession, *, agent: m.Agent, tenant_id: uuid.UUID
) -> list[LoadedSkill]:
    """The agent's enabled skills, narrowest assignment first. An agent in a
    department with an owner runs on the skills the owner confirmed
    (`connections.confirmation.held_skill_entries`), with the name and
    description they confirmed: a newer assignment, version or text waits
    for them, a removal applies at once."""
    # Deferred: confirmation reaches the agent package, which imports this module.
    from oc8.connections.confirmation import held_skill_entries

    rows: Sequence[tuple[m.SkillAssignment, m.SkillVersion | None, m.Skill | None]] = (
        (
            await db.execute(
                select(m.SkillAssignment, m.SkillVersion, m.Skill)
                .outerjoin(m.SkillVersion, m.SkillVersion.id == m.SkillAssignment.skill_version_id)
                .outerjoin(m.Skill, m.Skill.id == m.SkillVersion.skill_id)
                .where(
                    m.SkillAssignment.tenant_id == tenant_id,
                    m.SkillAssignment.enabled.is_(True),
                    # Three scopes, one query: what was given to this agent,
                    # what was given to its department, and what the tenant
                    # gives everyone. A standard set is turned on once and
                    # reaches whoever is hired afterwards without a second act.
                    or_(
                        m.SkillAssignment.agent_id == agent.id,
                        m.SkillAssignment.department_id == agent.department_id,
                        and_(
                            m.SkillAssignment.agent_id.is_(None),
                            m.SkillAssignment.department_id.is_(None),
                        ),
                    ),
                )
                # Narrowest first, so a skill an agent was given personally wins
                # the de-duplication below over the same skill from a set.
                .order_by(
                    m.SkillAssignment.agent_id.is_(None),
                    m.SkillAssignment.department_id.is_(None),
                    m.SkillAssignment.created_at,
                )
            )
        )
        .tuples()
        .all()
    )
    entries = await held_skill_entries(db, agent)
    if entries is not None:
        rows = await _held_rows(db, rows, entries, tenant_id)

    out: list[LoadedSkill] = []
    used: set[str] = set()
    # The SAME skill can now reach an agent by more than one route -- personally,
    # through its department, and through a tenant-wide set. Offering it three
    # times would fill the tool list with skill_x, skill_x_2 and skill_x_3, all
    # identical. The rows arrive narrowest-first, so the first one wins.
    seen_versions: set[uuid.UUID] = set()
    for assignment, version, skill in rows:
        if version is not None and version.id in seen_versions:
            continue
        if version is None or skill is None:
            # There is no FK on skill_assignment.skill_version_id, so an
            # assignment can outlive the version it points at (e.g. the
            # version's skill was deleted). Skip it -- with a warning --
            # rather than fail a run that is otherwise fine. A held version
            # (`_held_rows`) that cannot be read is skipped the same way.
            logger.warning(
                "skipping assignment %s: skill version %s not found",
                assignment.id,
                assignment.skill_version_id,
            )
            continue
        try:
            definition = parse_definition(version.definition or {})
        except SkillDefinitionError as exc:
            logger.warning("skipping skill %s (version %s): %s", skill.id, version.id, exc)
            continue
        # What the model reads of the skill: as the owner confirmed it, for
        # an agent whose skills are held (`held_skill_entries`).
        name, description = skill.name, skill.description
        if entries is not None:
            name, description = entries[str(assignment.id)][2:4]
            if name is None:
                logger.warning("skipping assignment %s: confirmed without a name", assignment.id)
                continue
        base = f"{SKILL_TOOL_PREFIX}{_slugify(definition.slug or name)}"
        tool_name = base
        suffix = 2
        while tool_name in used:
            tool_name = f"{base}_{suffix}"
            suffix += 1
        used.add(tool_name)
        seen_versions.add(version.id)
        out.append(
            LoadedSkill(
                skill_id=skill.id,
                skill_version_id=version.id,
                name=name,
                description=description or name,
                tool_name=tool_name,
                definition=definition,
                creator_id=skill.owner_user_id,
            )
        )
    return out


def skill_tool_schemas(skills: Sequence[LoadedSkill]) -> list[NeutralTool]:
    return [
        NeutralTool(
            name=s.tool_name,
            description=f"{s.name}: {s.description}",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        for s in skills
    ]


def catalog_block(skills: Sequence[LoadedSkill], *, prefix: str = "") -> str:
    """The catalogue an agent reads to know what it may load.

    ``prefix`` exists because a tool's name is not the same everywhere. In-process
    the model calls `skill_x`; through an MCP bridge the same tool is
    `mcp__<server>__skill_x`, and a catalogue that names the bare form sends the
    model looking for a tool that is not on its list. Observed live 2026-07-29: a
    lead read "(skill_auftrag_zerlegen)", could not find it, and reached for the
    harness's own generic `Skill` tool instead -- which does not reach oc8 at
    all, so the procedure never loaded and the work it describes never happened.
    """
    if not skills:
        return ""
    lines = "\n".join(f"- {s.name} ({prefix}{s.tool_name}): {s.description}" for s in skills)
    return (
        "Skills available to you. Call the named tool to load its procedure "
        f"before doing that kind of work:\n{lines}"
    )


def instruction_block(skill: LoadedSkill, satisfied: frozenset[str] | None = None) -> str:
    parts = [f"[Skill: {skill.name}]", skill.definition.instruction]
    if skill.definition.prose_guardrails:
        rules = "\n".join(f"- {g}" for g in skill.definition.prose_guardrails)
        parts.append(f"Rules you must follow:\n{rules}")
    if skill.definition.steps:
        parts.append(checklist(skill.definition.steps, satisfied or frozenset()))
    return "\n\n".join(parts)
