"""Whether a system is usable for an agent in a department (connections design).

One rule for every place that asks, so the agent builder, its ideas and the
runtime cannot drift apart:

- legacy: the department's frame enables the system and a connected
  McpConnection of that name exists, tenant-wide or in the department;
- integration: the plugin is an enabled integration, the department's frame
  does not switch its tool off, and an agent without an access row can act
  through it (`organisation_access_ready`) -- exactly what
  `runtime_env.integration_connections` accepts.

A frame that does not list a tool at all does not block it: that is an
agent-exclusive grant (`authz.pdp.narrowing_within_frame`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.pdp import ToolPolicy
from oc8.connections.access import single_organisation_credential
from oc8.connections.integrations import Integration, installed_integrations


@dataclass(frozen=True)
class DepartmentSystem:
    """One system as a new agent in a department would see it."""

    key: str  # the tool key agents' policies use
    plugin: str | None  # the plugin providing it, when known
    usable: bool
    #: Why it is not usable (English, for the Copilot); None when it is.
    missing: str | None = None


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
    if integration.credential_type and await _organisation_logins(
        db, integration.credential_type
    ):
        return (
            "installed, more than one organisation login -- an agent needs "
            "one chosen on its page after it is created"
        )
    return "installed, no organisation login yet -- connect it on /connections"


async def department_systems(
    db: AsyncSession, *, tenant_id: uuid.UUID, department: m.Department
) -> list[DepartmentSystem]:
    """Every system a new agent in `department` could use, and every
    installed integration it cannot (with the reason), sorted by key. Call
    with a tenant-bound session (installed integrations are read under RLS)."""
    frame = department.frame
    out: dict[str, DepartmentSystem] = {}
    rows = (
        await db.execute(
            select(
                m.McpConnection.name,
                m.McpConnection.config["_plugin_name"].as_string(),
            ).where(
                m.McpConnection.tenant_id == tenant_id,
                m.McpConnection.connected.is_(True),
                or_(
                    m.McpConnection.department_id.is_(None),
                    m.McpConnection.department_id == department.id,
                ),
            )
        )
    ).all()
    for name, plugin in rows:
        if frame_enables(frame, name):
            out.setdefault(name, DepartmentSystem(name, plugin or None, usable=True))
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
