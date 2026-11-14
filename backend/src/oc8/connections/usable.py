"""Whether a system is usable for an agent in a department (connections design).

One rule for every place that asks, so the agent builder, its ideas and the
runtime cannot drift apart:

- legacy: the department's frame enables the system and a connected
  McpConnection of that name is bound to the department
  (`department_connections`, the very query the MCP gateway hands a run). A
  connected one bound to no department reaches no run until it is given one
  (`needs_department`), so it is reported as missing, with that reason;
- integration: the plugin is an enabled integration, the department's frame
  does not switch its tool off, and an agent without an access row can act
  through it (`organisation_access_ready`) -- exactly what
  `runtime_env.integration_connections` accepts.

A frame that does not list a tool at all does not block it: that is an
agent-exclusive grant (`authz.pdp.narrowing_within_frame`).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.pdp import ToolPolicy
from oc8.capas.materialise import unbound_connection
from oc8.connections.access import single_organisation_credential
from oc8.connections.integrations import (
    Integration,
    enabled_tool_pack_connection,
    installed_integrations,
    integration_for_connection,
)

#: Why a connected system in no department is not usable (for the Copilot).
NO_DEPARTMENT = (
    "connected, but in no department -- assign it to a department on its connection under /capas"
)


@dataclass(frozen=True)
class DepartmentSystem:
    """One system as a new agent in a department would see it."""

    key: str  # the tool key agents' policies use
    plugin: str | None  # the plugin providing it, when known
    usable: bool
    #: Why it is not usable (English, for the Copilot); None when it is.
    missing: str | None = None


def department_connections(department_id: uuid.UUID) -> Select[tuple[m.McpConnection]]:
    """The connected systems bound to one department, oldest first: what the
    MCP gateway hands every run of an agent in it (integrations aside)."""
    return (
        select(m.McpConnection)
        .where(
            m.McpConnection.department_id == department_id,
            m.McpConnection.connected.is_(True),
        )
        .order_by(m.McpConnection.created_at)
    )


def needs_department(conn: m.McpConnection) -> bool:
    """A connection no run reaches until it is bound to a department: not an
    organisation login (pinned to agents one by one) and not an integration
    (`runtime_env.integration_connections` hands those out unbound)."""
    return (
        conn.department_id is None
        and conn.credential_id is None
        and integration_for_connection(conn) is None
    )


async def organisation_access_ready(db: AsyncSession, integration: Integration) -> bool:
    """Whether an agent WITHOUT an access row can act through `integration`:
    a sign-in integration always can (it acts as the person, user mode); a
    login type only with exactly one organisation login to fall back on."""
    if integration.kind == "oauth":
        return True
    return bool(
        integration.credential_type
        and await single_organisation_credential(db, integration.credential_type)
    )


def _frame_tools(frame: dict[str, Any] | None) -> dict[str, Any]:
    tools = (frame or {}).get("tools")
    return tools if isinstance(tools, dict) else {}


def frame_enables(frame: dict[str, Any] | None, key: str) -> bool:
    raw = _frame_tools(frame).get(key)
    return isinstance(raw, dict) and ToolPolicy.from_json(raw).enabled


def frame_switches_off(frame: dict[str, Any] | None, key: str) -> bool:
    """The frame lists `key` and does not enable it -- the only frame state
    that blocks an integration (an absent key is an agent-exclusive grant)."""
    tools = _frame_tools(frame)
    return key in tools and not frame_enables(frame, key)


async def _organisation_logins(db: AsyncSession, credential_type: str) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(m.Credential)
            .where(
                m.Credential.credential_type == credential_type,
                m.Credential.owner_member_id.is_(None),
            )
        )
        or 0
    )


async def _why_not(db: AsyncSession, integration: Integration) -> str:
    if integration.credential_type and await _organisation_logins(db, integration.credential_type):
        return (
            "installed, more than one organisation login -- an agent needs "
            "one chosen on its page after it is created"
        )
    return "installed, no organisation login yet -- connect it on /connections"


def _plugin(conn: m.McpConnection) -> str | None:
    cfg = conn.config if isinstance(conn.config, dict) else {}
    return str(cfg.get("_plugin_name") or "") or None


async def department_systems(
    db: AsyncSession, *, tenant_id: uuid.UUID, department: m.Department
) -> list[DepartmentSystem]:
    """Every system a new agent in `department` could use, and every
    installed integration it cannot (with the reason), sorted by key. Call
    with a tenant-bound session (installed integrations are read under RLS)."""
    frame = department.frame
    out: dict[str, DepartmentSystem] = {}
    for conn in (await db.execute(department_connections(department.id))).scalars():
        if frame_enables(frame, conn.name):
            out.setdefault(conn.name, DepartmentSystem(conn.name, _plugin(conn), usable=True))
    unbound = (
        await db.execute(
            select(m.McpConnection).where(
                m.McpConnection.tenant_id == tenant_id,
                m.McpConnection.connected.is_(True),
                m.McpConnection.department_id.is_(None),
            )
        )
    ).scalars()
    for conn in unbound:
        if conn.name not in out and frame_enables(frame, conn.name) and needs_department(conn):
            out[conn.name] = DepartmentSystem(
                conn.name, _plugin(conn), usable=False, missing=NO_DEPARTMENT
            )
    for installed in await installed_integrations(db):
        integration = installed.integration
        key = integration.tool_key
        if key in out and out[key].usable:
            continue
        if not installed.available:
            missing: str | None = "installed, not enabled -- enable it on /capas"
        elif frame_switches_off(frame, key):
            missing = "switched off for this department"
        elif await organisation_access_ready(db, integration):
            missing = None
        else:
            missing = await _why_not(db, integration)
        out[key] = DepartmentSystem(key, integration.name, usable=missing is None, missing=missing)
    return [out[key] for key in sorted(out)]


# ------------------------------------------------------------------ a member's
# The person behind a Copilot conversation (copilot direct access §4). There is
# no per-person ACL: a connection reaches a person exactly where it reaches a
# department they belong to. "Belong" is a live seat or `all_departments`.


def _tenant_departments(member: m.OrgMember) -> Select[tuple[m.Department]]:
    return select(m.Department).where(
        m.Department.tenant_id == member.tenant_id,
        m.Department.deleted_at.is_(None),
        m.Department.is_assistant_department.is_(False),
    )


async def member_departments_for(
    db: AsyncSession, *, member_id: uuid.UUID, tool_key: str
) -> list[tuple[uuid.UUID, ToolPolicy]]:
    """The departments the member belongs to whose frame enables `tool_key`,
    with that frame's policy for it, ordered by department name. Never the
    Copilot's own department."""
    member = await db.get(m.OrgMember, member_id)
    if member is None or member.deleted_at is not None:
        return []
    query = _tenant_departments(member)
    if not member.all_departments:
        seated = select(m.OrgMemberDepartment.department_id).where(
            m.OrgMemberDepartment.member_id == member.id,
            m.OrgMemberDepartment.revoked_at.is_(None),
        )
        query = query.where(m.Department.id.in_(seated))
    out: list[tuple[uuid.UUID, ToolPolicy]] = []
    for dept in (await db.execute(query.order_by(m.Department.name, m.Department.id))).scalars():
        if frame_enables(dept.frame, tool_key):
            out.append((dept.id, ToolPolicy.from_json(_frame_tools(dept.frame)[tool_key])))
    return out


async def tool_key_connected(
    db: AsyncSession, *, tool_key: str, department_ids: Sequence[uuid.UUID]
) -> bool:
    """A connected row for the key in one of these departments or in none, or
    an enabled integration that provides it."""
    row = await db.scalar(
        select(m.McpConnection.id)
        .where(
            m.McpConnection.name == tool_key,
            m.McpConnection.connected.is_(True),
            m.McpConnection.department_id.in_(list(department_ids))
            | m.McpConnection.department_id.is_(None),
        )
        .limit(1)
    )
    if row is not None:
        return True
    return await enabled_tool_pack_connection(db, tool_key) is not None


async def member_may_use(
    db: AsyncSession, *, member_id: uuid.UUID, tool_key: str, right: str
) -> bool:
    """§4, the one rule: the system is connected, a department the member
    belongs to enables it, and that department grants `right` ("read" or
    "modify") for it.

    The right is read from the department FRAME's per-key policy, not from
    `authz.authority.granted_for_member`: the human permission catalogue has no
    per-department tool rights (`tool:read` / `tool:write` are an agent's, and
    only an org admin holds them as a person), so it cannot say what a member
    may do through one department's connection. The frame is where an
    organisation states exactly that, per department and per system.
    """
    granting = [
        dept
        for dept, policy in await member_departments_for(db, member_id=member_id, tool_key=tool_key)
        if policy.has_right(right)
    ]
    return bool(granting) and await tool_key_connected(
        db, tool_key=tool_key, department_ids=granting
    )


async def member_usable_tool_keys(db: AsyncSession, *, member_id: uuid.UUID) -> list[str]:
    """Every key the member may read through, sorted: what the Copilot is told
    it may offer."""
    member = await db.get(m.OrgMember, member_id)
    if member is None:
        return []
    keys = {
        str(key)
        for dept in (await db.execute(_tenant_departments(member))).scalars()
        for key in _frame_tools(dept.frame)
    }
    return [
        key
        for key in sorted(keys)
        if await member_may_use(db, member_id=member_id, tool_key=key, right="read")
    ]


async def direct_connection(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    tool_key: str,
    department_ids: Sequence[uuid.UUID],
) -> m.McpConnection | None:
    """The row a direct-access run launches for `tool_key`: a connected one of
    the member's departments (oldest first), else a connected department-less
    one, else the enabled integration's row: its own department-less row, or
    one materialised now. Never another department's: it carries that
    department's identity."""
    candidates = (
        (
            await db.execute(
                select(m.McpConnection)
                .where(
                    m.McpConnection.tenant_id == tenant_id,
                    m.McpConnection.name == tool_key,
                    m.McpConnection.department_id.in_(list(department_ids))
                    | m.McpConnection.department_id.is_(None),
                )
                .order_by(m.McpConnection.created_at)
            )
        )
        .scalars()
        .all()
    )
    for conn in candidates:
        if conn.connected and conn.department_id is not None:
            return conn
    for conn in candidates:
        if conn.connected:
            return conn
    template = await enabled_tool_pack_connection(db, tool_key)
    if template is None:
        return None
    for conn in candidates:
        if conn.department_id is None and conn.credential_id is None:
            return conn
    manifest, pack_conn, _integration = template
    created = unbound_connection(tenant_id=tenant_id, manifest=manifest, conn=pack_conn)
    db.add(created)
    await db.flush()
    return created


async def tool_key_label(db: AsyncSession, tool_key: str) -> str:
    """What a person calls the system: the integration's label, else the key."""
    template = await enabled_tool_pack_connection(db, tool_key)
    return template[2].label if template is not None else tool_key
