"""Whether a system is usable for an agent in a department (connections design).

One rule for every place that asks, so the agent builder, its ideas and the
runtime cannot drift apart:

- legacy: the department's frame enables the system and a connected
  McpConnection of that name is bound to the department
  (`department_connections`, the very query the MCP gateway hands a run). A
  connected one bound to no department reaches no run until it is given one
  (`needs_department`), so it is reported as missing, with that reason;
- integration: the plugin is an enabled integration, the department's frame
  does not switch its tool off, and a login resolves for the department
  (`connections.login.offer_problem`: enabled for it, or the organisation's
  single login) -- exactly what `runtime_env.integration_connections` and the
  Copilot's direct access accept. The tool's one row
  (`connections.tool_connection`) carries no department; the frame decides
  who reaches it.

A frame that does not list a tool at all does not block it: that is an
agent-exclusive grant (`authz.pdp.narrowing_within_frame`).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.pdp import ToolPolicy
from oc8.capas.discovery import resolve_tool_pack_connection
from oc8.capas.materialise import unbound_connection
from oc8.connections.access import ConnectionRequired
from oc8.connections.integrations import (
    Integration,
    enabled_tool_pack_connection,
    installed_integrations,
    integration_enabled,
    integration_for_connection,
)
from oc8.connections.login import offer_problem
from oc8.connections.tool_connection import not_retired, tool_connection
from oc8.departments.repo import member_departments

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
    """The connected systems a department still owns, oldest first: a custom
    server bound to it, or a tool setup not consolidated yet. What the MCP
    gateway hands every run of an agent in it; an integration's shared row
    reaches the run through the policy (`runtime_env.integration_connections`)."""
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


async def offer_connection(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    integration: Integration,
    department_ids: Sequence[uuid.UUID],
) -> m.McpConnection | None:
    """The row a call of `integration` would run on for these departments
    (`tool_connection`, connected or not), else an unsaved one from the
    enabled plugin's manifest -- only to ask the resolver, never stored."""
    conn = await tool_connection(
        db, name=integration.tool_key, department_id=department_ids, connected_only=False
    )
    if conn is not None:
        return conn
    template = await enabled_tool_pack_connection(db, integration.tool_key)
    if template is None:
        return None
    manifest, pack_conn, _ = template
    return unbound_connection(tenant_id=tenant_id, manifest=manifest, conn=pack_conn)


async def login_offer_problem(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    integration: Integration,
    department_ids: Sequence[uuid.UUID],
    live_member_id: uuid.UUID | None = None,
) -> ConnectionRequired | None:
    """Why an agent (or person) in `department_ids` without an explicit
    choice cannot act through a login integration, None when a login
    resolves. A sign-in integration is always offered: it acts as the person,
    who is asked to sign in at the first call."""
    if integration.kind == "oauth":
        return None
    conn = await offer_connection(
        db, tenant_id=tenant_id, integration=integration, department_ids=department_ids
    )
    if conn is None:
        return ConnectionRequired(integration, "not_configured")
    return await offer_problem(
        db,
        integration=integration,
        conn=conn,
        department_ids=department_ids,
        live_member_id=live_member_id,
    )


def _frame_tools(frame: dict[str, Any] | None) -> dict[str, Any]:
    tools = (frame or {}).get("tools")
    return tools if isinstance(tools, dict) else {}


def frame_enables(frame: dict[str, Any] | None, key: str) -> bool:
    raw = _frame_tools(frame).get(key)
    return isinstance(raw, dict) and ToolPolicy.from_json(raw).enabled


def frame_switches_off(frame: dict[str, Any] | None, key: str) -> bool:
    """The frame lists `key` and does not enable it -- the only frame state
    that blocks an integration (an absent key is an agent-exclusive grant,
    unless the frame is closed, as a personal department's is)."""
    tools = _frame_tools(frame)
    if key not in tools and (frame or {}).get("closed") is True:
        return True  # a closed frame has no agent-exclusive grants
    return key in tools and not frame_enables(frame, key)


def _why_not(problem: ConnectionRequired) -> str:
    if problem.reason == "ambiguous":
        return (
            "installed, more than one organisation login enabled for this "
            "department -- an agent needs one chosen on its page after it is created"
        )
    return (
        "installed, no organisation login yet for this department -- connect it "
        "on /connections and enable it for the department"
    )


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
        else:
            problem = await login_offer_problem(
                db, tenant_id=tenant_id, integration=integration, department_ids=[department.id]
            )
            missing = None if problem is None else _why_not(problem)
        out[key] = DepartmentSystem(key, integration.name, usable=missing is None, missing=missing)
    return [out[key] for key in sorted(out)]


async def system_tool_scopes(
    db: AsyncSession, *, tenant_id: uuid.UUID, key: str
) -> dict[str, list[str]]:
    """A system's tool names by what they do (`read`, `modify`, ...), from its
    plugin manifest -- no call to the system. `credential_id.is_(None)` picks
    the manifest row, never a login sharing the name (as
    `GET /mcp/connections/{name}/tool-names` does). Empty when the plugin is
    gone or ships no tool pack."""
    conn = (
        await db.execute(
            select(m.McpConnection)
            .where(
                m.McpConnection.tenant_id == tenant_id,
                m.McpConnection.name == key,
                m.McpConnection.credential_id.is_(None),
                not_retired(),
            )
            .order_by(m.McpConnection.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    cfg = conn.config if conn is not None and isinstance(conn.config, dict) else {}
    manifest_conn = resolve_tool_pack_connection(
        str(cfg.get("_plugin_name", "")), str(cfg.get("_connection_key", ""))
    )
    if manifest_conn is None or not isinstance(manifest_conn.scopes, dict):
        return {}
    return {
        str(scope): [str(name) for name in names]
        for scope, names in manifest_conn.scopes.items()
        if isinstance(names, list)
    }


# ------------------------------------------------------------------ a member's
# The person behind a Copilot conversation (copilot direct access §4). There is
# no per-person ACL: a connection reaches a person exactly where it reaches a
# department they belong to. "Belong" is a live seat or `all_departments`.


async def member_departments_for(
    db: AsyncSession, *, member_id: uuid.UUID, tool_key: str
) -> list[tuple[uuid.UUID, ToolPolicy]]:
    """The departments the member belongs to whose frame enables `tool_key`,
    with that frame's policy for it, ordered by department name. Never the
    Copilot's own department."""
    member = await db.get(m.OrgMember, member_id, populate_existing=True)
    if member is None or member.deleted_at is not None:
        return []
    out: list[tuple[uuid.UUID, ToolPolicy]] = []
    for dept in await member_departments(db, member=member):
        if frame_enables(dept.frame, tool_key):
            out.append((dept.id, ToolPolicy.from_json(_frame_tools(dept.frame)[tool_key])))
    return out


async def tool_key_connected(
    db: AsyncSession,
    *,
    tool_key: str,
    department_ids: Sequence[uuid.UUID],
    member_id: uuid.UUID,
) -> bool:
    """Whether the member can act through `tool_key` lent by one of these
    departments: a login integration only when a login resolves for them (the
    resolver's order: their own, one enabled for these departments, the
    organisation's single one) -- so an offer is never approved and then
    refused; a sign-in integration whenever it is enabled (the person signs
    in at the first call); another system when it has a connected row the
    departments reach (`tool_connection`)."""
    if not department_ids:
        return False
    conn = await tool_connection(db, name=tool_key, department_id=department_ids)
    integration = integration_for_connection(conn) if conn is not None else None
    if conn is not None and integration is None:
        return True
    if integration is None:
        template = await enabled_tool_pack_connection(db, tool_key)
        if template is None:
            return False
        integration = template[2]
    elif not await integration_enabled(db, integration):
        return False
    if integration.kind == "oauth":
        # The same rule as every other offer (`login_offer_problem`,
        # `runtime_env.integration_connections`): a sign-in integration acts
        # as the person, and its sign-in prompt is how they connect.
        return True
    if conn is None:
        member = await db.get(m.OrgMember, member_id)
        if member is None:
            return False
        conn = await offer_connection(
            db, tenant_id=member.tenant_id, integration=integration, department_ids=department_ids
        )
        if conn is None:
            return False
    return (
        await offer_problem(
            db,
            integration=integration,
            conn=conn,
            department_ids=department_ids,
            live_member_id=member_id,
        )
        is None
    )


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
        db, tool_key=tool_key, department_ids=granting, member_id=member_id
    )


async def member_usable_tool_keys(db: AsyncSession, *, member_id: uuid.UUID) -> list[str]:
    """Every key the member may read through, sorted: what the Copilot is told
    it may offer."""
    member = await db.get(m.OrgMember, member_id, populate_existing=True)
    if member is None:
        return []
    keys = {
        str(key)
        for dept in await member_departments(db, member=member)
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
    departments: Sequence[tuple[uuid.UUID, ToolPolicy]],
    right: str,
) -> m.McpConnection | None:
    """The row a direct-access call of `right` launches for `tool_key`.

    The tool's one row (`tool_connection`): while unconsolidated, the oldest
    connected row of a department whose policy grants `right` -- the one that
    authorised the call -- never another department's of the member's, whose
    row carries that department's identity; else the shared connected row;
    else the enabled integration's own department-less row, or one
    materialised now. None otherwise. Which login it runs with is the
    resolver's (`resolve_direct_access`)."""
    granting = [dept for dept, policy in departments if policy.has_right(right)]
    if not granting:
        return None
    conn = await tool_connection(db, name=tool_key, department_id=granting)
    if conn is not None:
        return conn
    template = await enabled_tool_pack_connection(db, tool_key)
    if template is None:
        return None
    shared = await tool_connection(db, name=tool_key, department_id=None, connected_only=False)
    if shared is not None:
        return shared
    manifest, pack_conn, _integration = template
    created = unbound_connection(tenant_id=tenant_id, manifest=manifest, conn=pack_conn)
    db.add(created)
    await db.flush()
    return created


async def tool_key_label(db: AsyncSession, tool_key: str) -> str:
    """What a person calls the system: the integration's label, else the key."""
    template = await enabled_tool_pack_connection(db, tool_key)
    return template[2].label if template is not None else tool_key
