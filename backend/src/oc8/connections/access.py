"""Which account a run's tool call acts through (connections design).

`legacy`: the McpConnection's own configuration, unchanged (a department's
app-only setup, a pinned login, a tool that is no connectable integration).
`oauth` / `credential`: one OAuth connection or credential, chosen by the
agent's access row or the defaults below. Anything else raises
ConnectionRequired: there is no fallback to another person's account or the
organisation's.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.connections.acting import ActingPerson, resolve_acting_person
from oc8.connections.integrations import Integration, integration_for_connection


class ConnectionRequired(Exception):
    """The call needs an account that is not available. `str()` is for the
    model; `as_context()` is what the run records for the UI."""

    def __init__(self, integration: Integration, reason: str) -> None:
        self.integration = integration
        self.reason = reason
        super().__init__(self._message())

    def _message(self) -> str:
        what = (
            f"the person's {self.integration.label} account"
            if self.integration.kind == "oauth"
            else f"a {self.integration.label} login"
        )
        if self.reason == "schedule_without_owner":
            return (
                f"connection_required: this schedule has no owner, "
                f"so it cannot act through {what}. "
                "It must be saved once again by the person whose account it should use."
            )
        if self.reason == "no_acting_person":
            return (
                f"connection_required: nobody is working with this run, "
                f"so {what} cannot be chosen. "
                "An administrator can give this agent a fixed access."
            )
        if self.reason == "needs_reauth":
            return (
                f"connection_required: {what} must be connected again. "
                "Tell the user to reconnect it under Connections."
            )
        if self.reason == "binding_needs_confirmation":
            return (
                f"connection_required: this agent's access to {self.integration.label} uses a "
                "person's account and was not confirmed since the agent last changed. "
                "The account's owner or an administrator must save the access setting again."
            )
        if self.reason == "not_configured":
            return (
                f"connection_required: no access to {self.integration.label} "
                "is set up for this agent."
            )
        return (
            f"connection_required: this task needs {what}, which is not connected. "
            "Tell the user to connect it under Connections."
        )

    def as_context(self) -> dict[str, str | None]:
        return {
            "integration": self.integration.name,
            "provider": self.integration.provider,
            "label": self.integration.label,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ToolAccess:
    kind: str  # "legacy" | "oauth" | "credential"
    integration: Integration | None
    on_behalf_of: uuid.UUID | None
    oauth_connection: m.OAuthConnection | None = None
    credential: m.Credential | None = None


async def single_organisation_credential(
    db: AsyncSession, credential_type: str
) -> m.Credential | None:
    rows = (
        (
            await db.execute(
                select(m.Credential)
                .where(
                    m.Credential.credential_type == credential_type,
                    m.Credential.owner_member_id.is_(None),
                )
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    return rows[0] if len(rows) == 1 else None


async def resolve_tool_access(
    db: AsyncSession, *, run: m.AgentRun, agent: m.Agent, conn: m.McpConnection
) -> ToolAccess:
    acting = await resolve_acting_person(db, run)
    integration = integration_for_connection(conn)
    if integration is None:
        return ToolAccess("legacy", None, acting.member_id)
    row = await db.scalar(
        select(m.AgentToolAccess).where(
            m.AgentToolAccess.agent_id == agent.id, m.AgentToolAccess.tool_key == conn.name
        )
    )
    if row is not None and row.mode == "fixed":
        return await _fixed(db, row, agent, integration, acting)
    if row is None:
        if conn.credential_id is not None:
            return ToolAccess("legacy", integration, acting.member_id)
        default = await _default(db, conn, integration, acting)
        if default is not None:
            return default
    return await _user(db, integration, acting)


def _consented(row: m.AgentToolAccess, agent: m.Agent, owner: uuid.UUID | None) -> bool:
    """A person's own account may only be acted through when that person (or an
    administrator) set the binding, and the agent is unchanged since."""
    if owner is None:
        return True
    basis = row.set_with_manage or (
        row.set_by_member_id is not None and row.set_by_member_id == owner
    )
    return basis and row.agent_revision is not None and row.agent_revision == agent.config_revision


async def _fixed(
    db: AsyncSession,
    row: m.AgentToolAccess,
    agent: m.Agent,
    integration: Integration,
    acting: ActingPerson,
) -> ToolAccess:
    if integration.kind == "oauth" and row.oauth_connection_id is not None:
        target = await db.get(m.OAuthConnection, row.oauth_connection_id)
        if target is not None and target.provider == integration.provider:
            if target.status != "active":
                raise ConnectionRequired(integration, "needs_reauth")
            if not _consented(row, agent, target.owner_member_id):
                raise ConnectionRequired(integration, "binding_needs_confirmation")
            return ToolAccess("oauth", integration, acting.member_id, oauth_connection=target)
    if integration.kind == "credential" and row.credential_id is not None:
        cred = await db.get(m.Credential, row.credential_id)
        if cred is not None and cred.credential_type == integration.credential_type:
            if not _consented(row, agent, cred.owner_member_id):
                raise ConnectionRequired(integration, "binding_needs_confirmation")
            return ToolAccess("credential", integration, acting.member_id, credential=cred)
    # The target is gone (ON DELETE SET NULL) or never matched: refuse, never re-choose.
    raise ConnectionRequired(integration, "not_connected")


async def _default(
    db: AsyncSession, conn: m.McpConnection, integration: Integration, acting: ActingPerson
) -> ToolAccess | None:
    cfg = conn.config if isinstance(conn.config, dict) else {}
    if conn.department_id is not None and (
        integration.kind == "credential" or cfg.get("oauth_connection_id")
    ):
        # A department's existing setup (app-only app registration, or the
        # setup form's login): exactly what ran before connections existed.
        return ToolAccess("legacy", integration, acting.member_id)
    if integration.kind == "credential":
        assert integration.credential_type is not None
        only = await single_organisation_credential(db, integration.credential_type)
        if only is not None:
            return ToolAccess("credential", integration, acting.member_id, credential=only)
        raise ConnectionRequired(integration, "not_configured")
    return None  # an OAuth integration without a department setup: user mode


async def _user(db: AsyncSession, integration: Integration, acting: ActingPerson) -> ToolAccess:
    if acting.member_id is None:
        raise ConnectionRequired(integration, acting.reason or "no_acting_person")
    if integration.kind == "oauth":
        rows = (
            (
                await db.execute(
                    select(m.OAuthConnection)
                    .where(
                        m.OAuthConnection.provider == integration.provider,
                        m.OAuthConnection.owner_member_id == acting.member_id,
                        m.OAuthConnection.grant_type == "authorization_code",
                    )
                    .order_by(m.OAuthConnection.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        active = next((r for r in rows if r.status == "active"), None)
        if active is None:
            raise ConnectionRequired(integration, "needs_reauth" if rows else "not_connected")
        return ToolAccess("oauth", integration, acting.member_id, oauth_connection=active)
    cred = await db.scalar(
        select(m.Credential)
        .where(
            m.Credential.credential_type == integration.credential_type,
            m.Credential.owner_member_id == acting.member_id,
        )
        .order_by(m.Credential.created_at.desc())
        .limit(1)
    )
    if cred is None:
        raise ConnectionRequired(integration, "not_connected")
    return ToolAccess("credential", integration, acting.member_id, credential=cred)
