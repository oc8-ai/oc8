"""Shipped skills an admin may edit in place.

A skill a capa ships, or one imported from a store, carries `origin="store"`
and keeps a pointer to the version holding the content as it was shipped
(`Skill.shipped_version_id`). An admin may change that content directly: the
edit is a new version (copy on write, the shipped row is never mutated) and
`Skill.customized_at` records that it happened.

The shipped side follows the same rule the built-in Assistant's mission does
(`oc8.agent.assistant._sync_mission`): a skill still on its shipped version
follows a shipped update; a customised one is never overwritten -- the newer
shipped content is stored beside the edit and `Skill.shipped_update_at` says it
is waiting, so a reset can take it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m

#: Definition keys that change with every release of the shipping capa without
#: the skill's content changing -- a capa bump alone is not a new skill.
_RELEASE_KEYS = frozenset({"version"})


def artifact_hash(definition: dict[str, Any]) -> bytes:
    return hashlib.sha256(
        json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _content_hash(definition: dict[str, Any]) -> bytes:
    return artifact_hash({k: v for k, v in definition.items() if k not in _RELEASE_KEYS})


def is_customized(skill: m.Skill) -> bool:
    """Changed away from its shipped content -- flagged, or (for a skill edited
    before the flag existed) simply no longer on the shipped version."""
    if skill.customized_at is not None:
        return True
    return skill.shipped_version_id is not None and (
        skill.current_version_id != skill.shipped_version_id
    )


def needs_copy_on_write(skill: m.Skill) -> bool:
    """True when the current version IS the shipped content, so an edit must
    land in a new version rather than change that row."""
    return skill.origin == "store" and (
        skill.shipped_version_id is None or skill.current_version_id == skill.shipped_version_id
    )


def mark_customized(skill: m.Skill, *, previous_version_id: Any) -> None:
    """Record an in-place edit of a store skill. A store skill that predates the
    shipped-version record (seeded, or imported before it existed) takes the
    version it had until this edit as its shipped one."""
    if skill.origin != "store":
        return
    if skill.shipped_version_id is None:
        skill.shipped_version_id = previous_version_id
    if skill.customized_at is None:
        skill.customized_at = dt.datetime.now(tz=dt.UTC)


async def _free_semver(db: AsyncSession, skill: m.Skill, semver: str) -> str:
    taken = set(
        (await db.execute(select(m.SkillVersion.semver).where(m.SkillVersion.skill_id == skill.id)))
        .scalars()
        .all()
    )
    if semver not in taken:
        return semver
    n = 1
    while f"{semver}+{n}" in taken:
        n += 1
    return f"{semver}+{n}"


async def sync_shipped_skill(
    db: AsyncSession,
    skill: m.Skill,
    *,
    definition: dict[str, Any],
    semver: str,
    description: str | None = None,
    category: str | None = None,
) -> None:
    """Bring a shipped skill's shipped content up to `definition`.

    Unchanged content adds no version. Changed content becomes a new version
    recorded as the shipped one; it also becomes current unless the skill is
    customised, in which case the edit stays live and `shipped_update_at` is
    set. Live assignments stay pinned either way, exactly as an admin's own
    publish leaves them unless asked to move them.

    An uncustomised skill also follows the shipped description and category.
    Never the name: the name is how the shipped skill is found again.
    """
    customized = is_customized(skill)
    if not customized:
        if description is not None and skill.description != description:
            skill.description = description
        if skill.category != category:
            skill.category = category
    shipped_id = skill.shipped_version_id or (None if customized else skill.current_version_id)
    shipped = await db.get(m.SkillVersion, shipped_id) if shipped_id is not None else None
    if shipped is not None and _content_hash(shipped.definition) == _content_hash(definition):
        if skill.shipped_version_id is None:
            skill.shipped_version_id = shipped.id
            await db.flush()
        return

    version = m.SkillVersion(
        tenant_id=skill.tenant_id,
        skill_id=skill.id,
        semver=await _free_semver(db, skill, semver),
        definition=definition,
        artifact_hash=artifact_hash(definition),
    )
    db.add(version)
    await db.flush()
    skill.shipped_version_id = version.id
    if customized:
        skill.shipped_update_at = dt.datetime.now(tz=dt.UTC)
    else:
        skill.current_version_id = version.id
    await db.flush()
