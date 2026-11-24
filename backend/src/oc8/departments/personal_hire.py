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
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
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
