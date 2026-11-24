"""The logins a member binds when they hire a personal template (plan Rulings
9 and 10).

A personal template names the capas it connects to: `connections_any` (the
member chooses one) and `connections_optional` (each one they may add). At
hire the member names a login of their own for each choice.
`resolve_hire_bindings` checks every choice before anything is written: the
template offers it, the capa is installed and enabled and its tools are
enabled in the personal frame, and the login is the member's own, of the
capa's kind, usable now. Organisation logins are refused here: a hire never
shares one. `instantiate_agent` then writes each binding through
`connections.access_write.write_tool_access`, the one writer of tool access.

The hire is the member's confirmation of what the agent may do, so it must be
the offer they were shown: `hire_offer` digests the enabled template version,
its setup values, the frame tools and the skills the agent would get into a
`hire_token` the templates list hands out and the hire checks again
(plan Ruling T8-I1).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.versioning import payload_hash
from oc8.capas.manifest import TemplateAgent
from oc8.connections.integrations import Integration, installed_integrations
from oc8.credentials.service import require_complete


@dataclass(frozen=True)
class ChosenLogin:
    """The member's choice for one capa the template names: their credential
    or their OAuth connection."""

    integration: str
    credential_id: uuid.UUID | None
    oauth_connection_id: uuid.UUID | None


@dataclass(frozen=True)
class HireBinding:
    """A checked choice: bind `integration`'s tools to this login."""

    integration: Integration
    credential_id: uuid.UUID | None
    oauth_connection_id: uuid.UUID | None


class HireBindingRefused(Exception):
    """A choice that cannot be bound. Routes answer 422 `{"error": error}`."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


@dataclass(frozen=True)
class TemplateConnection:
    """One capa a template names, as the member sees it before a hire."""

    integration: str
    label: str
    #: "credential" or "oauth"; "" for a capa that is not installed.
    kind: str
    provider: str | None
    credential_type: str | None
    available: bool


def _tool_enabled(frame_tools: Mapping[str, Any], tool_key: str) -> bool:
    policy = frame_tools.get(tool_key)
    return isinstance(policy, Mapping) and policy.get("enabled") is True


async def _available(db: AsyncSession, frame_tools: Mapping[str, Any]) -> dict[str, Integration]:
    """Ruling 10: the integrations a hire may bind, by capa name -- installed
    and enabled, with their tools enabled in the personal frame."""
    return {
        item.integration.name: item.integration
        for item in await installed_integrations(db)
        if item.available and _tool_enabled(frame_tools, item.integration.tool_key)
    }


async def template_connections(
    db: AsyncSession, *, frame_tools: Mapping[str, Any], names: Sequence[str]
) -> list[TemplateConnection]:
    """`names` in their order, each with whether a hire may bind it now."""
    installed = {item.integration.name: item for item in await installed_integrations(db)}
    out: list[TemplateConnection] = []
    for name in names:
        item = installed.get(name)
        if item is None:
            out.append(TemplateConnection(name, name, "", None, None, available=False))
            continue
        integ = item.integration
        out.append(
            TemplateConnection(
                integration=name,
                label=integ.label,
                kind=integ.kind,
                provider=integ.provider,
                credential_type=integ.credential_type,
                available=item.available and _tool_enabled(frame_tools, integ.tool_key),
            )
        )
    return out


async def _own_login(
    db: AsyncSession, *, member: m.OrgMember, integ: Integration, chosen: ChosenLogin
) -> HireBinding:
    """The member's own, usable login of `integ`'s kind, or refused."""
    if integ.kind == "credential":
        if chosen.credential_id is None or chosen.oauth_connection_id is not None:
            raise HireBindingRefused("login_not_usable")
        cred = await db.get(m.Credential, chosen.credential_id)
        if (
            cred is None
            or cred.tenant_id != member.tenant_id
            or cred.owner_member_id != member.id
            or cred.credential_type != integ.credential_type
        ):
            raise HireBindingRefused("login_not_usable")
        # 422 `credential_incomplete`, only once the login is known to be theirs.
        await require_complete(db, cred)
        return HireBinding(integ, cred.id, None)
    if chosen.oauth_connection_id is None or chosen.credential_id is not None:
        raise HireBindingRefused("login_not_usable")
    conn = await db.get(m.OAuthConnection, chosen.oauth_connection_id)
    if (
        conn is None
        or conn.tenant_id != member.tenant_id
        or conn.owner_member_id != member.id
        or conn.provider != integ.provider
        or conn.grant_type == "device_code"
    ):
        raise HireBindingRefused("login_not_usable")
    if conn.status != "active":
        raise HireBindingRefused("login_needs_reauth")
    return HireBinding(integ, None, conn.id)


async def resolve_hire_bindings(
    db: AsyncSession,
    *,
    member: m.OrgMember,
    frame_tools: Mapping[str, Any],
    spec: TemplateAgent,
    connection: ChosenLogin | None,
    optional: Sequence[ChosenLogin],
) -> list[HireBinding]:
    """The bindings a hire of `spec` writes for `member`, every choice
    checked; raises `HireBindingRefused` (or `IncompleteCredential`) before
    anything is written. `frame_tools`: the personal department's applied
    frame tools, or the tenant default before its first hire."""
    if not spec.connections_any and not spec.connections_optional:
        if connection is not None or optional:
            raise HireBindingRefused("connections_not_declared")
        return []
    if spec.connections_any and connection is None:
        raise HireBindingRefused("connection_required")
    choices = [connection] if connection is not None else []
    if connection is not None and connection.integration not in spec.connections_any:
        raise HireBindingRefused("connection_not_offered")
    seen: set[str] = set()
    for chosen in optional:
        if chosen.integration not in spec.connections_optional or chosen.integration in seen:
            raise HireBindingRefused("connection_not_offered")
        seen.add(chosen.integration)
    choices.extend(optional)
    available = await _available(db, frame_tools)
    bindings: list[HireBinding] = []
    for chosen in choices:
        integ = available.get(chosen.integration)
        if integ is None:
            raise HireBindingRefused("connection_unavailable")
        bindings.append(await _own_login(db, member=member, integ=integ, chosen=chosen))
    return bindings


@dataclass(frozen=True)
class HireOffer:
    """What a hire of one template grants now, and the token that pins it."""

    token: str
    #: The frame tools the agent may use (enabled ones), sorted.
    tools: list[str]
    #: The names of the skills the agent would run with, sorted.
    skills: list[str]


async def _offered_skills(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    skill_names: Sequence[str],
    department_id: uuid.UUID | None,
) -> dict[str, list[Any]]:
    """The skills an agent hired now would get, as `[skill_version_id,
    overrides, name, description]`: the template's own (resolved by name as
    `capas.service._assign_named_skills` does), and the enabled tenant-wide
    and department assignments (`connections.confirmation._grant_skills`)."""
    out: dict[str, list[Any]] = {}
    for name in skill_names:
        skill = (
            (
                await db.execute(
                    select(m.Skill)
                    .where(
                        m.Skill.tenant_id == tenant_id,
                        m.Skill.name == name,
                        m.Skill.deleted_at.is_(None),
                    )
                    .order_by(m.Skill.created_at)
                )
            )
            .scalars()
            .first()
        )
        if skill is not None and skill.current_version_id is not None:
            out[f"template:{name}"] = [
                str(skill.current_version_id),
                None,
                skill.name,
                skill.description,
            ]
    scope = m.SkillAssignment.agent_id.is_(None) & m.SkillAssignment.department_id.is_(None)
    if department_id is not None:
        scope = scope | (m.SkillAssignment.department_id == department_id)
    rows = (
        await db.execute(
            select(m.SkillAssignment, m.Skill.name, m.Skill.description)
            .outerjoin(m.SkillVersion, m.SkillVersion.id == m.SkillAssignment.skill_version_id)
            .outerjoin(m.Skill, m.Skill.id == m.SkillVersion.skill_id)
            .where(m.SkillAssignment.deleted_at.is_(None), scope)
        )
    ).tuples()
    for assignment, name, description in rows:
        if assignment.enabled:
            out[str(assignment.id)] = [
                str(assignment.skill_version_id),
                assignment.overrides,
                name,
                description,
            ]
    return out


async def hire_offer(
    db: AsyncSession,
    *,
    version: m.CapaVersion,
    spec: TemplateAgent,
    frame_tools: Mapping[str, Any],
    department_id: uuid.UUID | None,
) -> HireOffer:
    """The offer a hire of `version` makes now: the enabled template version,
    its setup values (they fill `{{key}}` in texts and schedules, Ruling
    T8-W1), the frame tools (`frame_tools`: the department's applied ones, or
    the tenant default before its first hire) and the skills. Any change to
    one of them changes the token."""
    from oc8.capas.service import load_installation_config

    config = await load_installation_config(db, capa_id=version.capa_id)
    skills = await _offered_skills(
        db, tenant_id=version.tenant_id, skill_names=spec.skills, department_id=department_id
    )
    token = payload_hash(
        {
            "version_id": str(version.id),
            "config": config,
            "frame_tools": dict(frame_tools),
            "skills": skills,
        }
    ).hex()
    return HireOffer(
        token=token,
        tools=sorted(key for key in frame_tools if _tool_enabled(frame_tools, key)),
        skills=sorted({str(entry[2]) for entry in skills.values() if entry[2] is not None}),
    )
