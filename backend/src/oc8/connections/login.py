"""Which tool login a call uses: one order for agents and the Copilot
(unified connections design, "Resolver order").

1. The agent's explicit choice (`AgentToolAccess`: a fixed login, or "the
   person's own"). A row pinned to a login (`McpConnection.credential_id`) is
   such a choice too, until it is consolidated.
2. In a live chat a person runs: their own login, if they have one.
   Scheduled and triggered runs skip this (`live_member_id=None`).
3. The organisation login enabled for one of `department_ids` (directly or
   through "all departments"): exactly one -> it; several -> `ambiguous`.
4. Only when no login of the type is enabled for any department at all (a
   tenant that never used enablement): the organisation's single login.
5. An unconsolidated department row that still carries copied login values
   serves its department (`legacy`); otherwise `not_configured`.

A sign-in (OAuth) integration takes steps 1 and 2, then the department's
app-only setup and user mode as before. Core-neutral: driven by the tool
pack's `credential_env_fields`, never by a vendor.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.connections.access import (
    ConnectionRequired,
    _fixed,
    _require_schedule_confirmed,
    _user,
    single_organisation_credential,
)
from oc8.connections.acting import ACTING_PERSON_WITHHELD, ActingPerson, resolve_acting_person
from oc8.connections.integrations import Integration
from oc8.credentials.enablement import enabled_credentials

Source = Literal["agent", "own", "department", "organisation", "legacy"]


@dataclass(frozen=True)
class LoginChoice:
    credential: m.Credential | None
    source: Source
    legacy_conn: m.McpConnection | None
    #: A sign-in integration's chosen account (steps 1-2 or user mode).
    oauth_connection: m.OAuthConnection | None = None


def live_chat_member(run: m.AgentRun | None, acting: ActingPerson) -> uuid.UUID | None:
    """The person present in a live web chat of any agent, or None.

    The same test as `direct_access.direct_access_run`, for every agent: a
    chat turn through the web door, never a follow-up, a triggered or
    delegated run, or one whose person was withheld. The person is the one
    the run acts for (`on_behalf_of`, else the session's own member writing
    their own words -- `resolve_acting_person`)."""
    if run is None or run.source != "chat":
        return None
    ctx = run.context if isinstance(run.context, dict) else {}
    if ctx.get(ACTING_PERSON_WITHHELD) or ctx.get("followup") or ctx.get("trigger_id"):
        return None
    if ctx.get("parent_task_id") or ctx.get("door") != "web" or not ctx.get("chat_session_id"):
        return None
    return acting.member_id


async def own_login(
    db: AsyncSession, integration: Integration, member_id: uuid.UUID
) -> LoginChoice | None:
    """The member's own newest login of the type (or active sign-in), if any."""
    if integration.kind == "oauth":
        active = await db.scalar(
            select(m.OAuthConnection)
            .where(
                m.OAuthConnection.provider == integration.provider,
                m.OAuthConnection.owner_member_id == member_id,
                m.OAuthConnection.grant_type == "authorization_code",
                m.OAuthConnection.status == "active",
            )
            .order_by(m.OAuthConnection.created_at.desc())
            .limit(1)
        )
        return None if active is None else LoginChoice(None, "own", None, oauth_connection=active)
    cred = await db.scalar(
        select(m.Credential)
        .where(
            m.Credential.credential_type == integration.credential_type,
            m.Credential.owner_member_id == member_id,
        )
        .order_by(m.Credential.created_at.desc())
        .limit(1)
    )
    return None if cred is None else LoginChoice(cred, "own", None)


async def enabled_anywhere(db: AsyncSession, credential_type: str) -> bool:
    """Whether any organisation login of the type is enabled for a department
    (a row in `credential_department`, or `all_departments`) in this tenant."""
    enabled = select(m.CredentialDepartment.id).where(
        m.CredentialDepartment.credential_id == m.Credential.id
    )
    found = await db.scalar(
        select(m.Credential.id)
        .where(
            m.Credential.credential_type == credential_type,
            m.Credential.owner_member_id.is_(None),
            or_(m.Credential.all_departments.is_(True), exists(enabled)),
        )
        .limit(1)
    )
    return found is not None


def _cfg(conn: m.McpConnection) -> dict[str, Any]:
    return conn.config if isinstance(conn.config, dict) else {}


def holds_copied_login(conn: m.McpConnection, integration: Integration) -> bool:
    """An unconsolidated department row: it belongs to a department, pins no
    login, and its env still holds a value the login type maps onto."""
    if conn.department_id is None or conn.credential_id is not None:
        return False
    cfg = _cfg(conn)
    env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    secret_env = cfg.get("secret_env") if isinstance(cfg.get("secret_env"), dict) else {}
    return any(
        (env or {}).get(name) or (secret_env or {}).get(name)
        for name in integration.credential_env_fields
    )


def _app_only(conn: m.McpConnection, department_ids: Sequence[uuid.UUID]) -> bool:
    """A department's app-only sign-in setup on this row (OAuth)."""
    return (
        conn.department_id is not None
        and conn.department_id in department_ids
        and bool(_cfg(conn).get("oauth_connection_id"))
    )


async def _agent_choice(
    db: AsyncSession,
    *,
    run: m.AgentRun,
    agent: m.Agent,
    integration: Integration,
    conn: m.McpConnection,
    acting: ActingPerson,
) -> LoginChoice | None:
    row = await db.scalar(
        select(m.AgentToolAccess).where(
            m.AgentToolAccess.agent_id == agent.id, m.AgentToolAccess.tool_key == conn.name
        )
    )
    if row is None:
        if conn.credential_id is not None:
            # A row pinned to one login (`/mcp/logins`): the agent's choice,
            # until consolidation turns it into an access row.
            return LoginChoice(None, "legacy", conn)
        return None
    if row.mode == "fixed":
        access = await _fixed(db, row, agent, run, conn.name, integration, acting)
        return LoginChoice(
            access.credential, "agent", None, oauth_connection=access.oauth_connection
        )
    # mode "user": the acting person's own login, or connection required.
    return await _user_mode(db, run=run, integration=integration, acting=acting)


async def _user_mode(
    db: AsyncSession, *, run: m.AgentRun | None, integration: Integration, acting: ActingPerson
) -> LoginChoice:
    if run is not None and acting.member_id is not None:
        await _require_schedule_confirmed(db, run, integration, acting.member_id)
    access = await _user(db, integration, acting)
    return LoginChoice(access.credential, "own", None, oauth_connection=access.oauth_connection)


async def resolve_login(
    db: AsyncSession,
    *,
    agent: m.Agent | None,
    integration: Integration,
    conn: m.McpConnection,
    live_member_id: uuid.UUID | None,
    department_ids: Sequence[uuid.UUID],
    right: Literal["read", "modify"],
    run: m.AgentRun | None = None,
    acting: ActingPerson | None = None,
) -> LoginChoice:
    """The login a call through `conn` uses (module docstring for the order).

    `department_ids`: an agent's own department; for the Copilot, the
    person's departments whose frame grants `right` (empty: none lends a
    login, only the person's own may be used). `run`: the agent run, for the
    agent's explicit choice and its consent rules; None for a Copilot direct
    access call, which has no choice of its own (step 1 is skipped); `agent`
    may then be None."""
    if run is not None:
        assert agent is not None
        acting = acting if acting is not None else await resolve_acting_person(db, run)
        chosen = await _agent_choice(
            db, run=run, agent=agent, integration=integration, conn=conn, acting=acting
        )
        if chosen is not None:
            return chosen
    if live_member_id is not None:
        own = await own_login(db, integration, live_member_id)
        if own is not None:
            return own
    if integration.kind == "oauth":
        if _app_only(conn, department_ids):
            return LoginChoice(None, "legacy", conn)
        # User mode: the acting person's own sign-in, or why there is none.
        person = acting if run is not None and acting is not None else ActingPerson(live_member_id)
        return await _user_mode(db, run=run, integration=integration, acting=person)
    assert integration.credential_type is not None
    if not department_ids:
        # No department of the person's lends a login for this right.
        raise ConnectionRequired(integration, "not_connected")
    if conn.credential_id is not None:
        # A pinned row reached without an agent's choice (a Copilot call).
        return LoginChoice(None, "legacy", conn)
    enabled: dict[uuid.UUID, m.Credential] = {}
    for department_id in department_ids:
        for cred in await enabled_credentials(
            db, credential_type=integration.credential_type, department_id=department_id
        ):
            enabled.setdefault(cred.id, cred)
    if len(enabled) == 1:
        return LoginChoice(next(iter(enabled.values())), "department", None)
    if len(enabled) > 1:
        raise ConnectionRequired(integration, "ambiguous")
    if not await enabled_anywhere(db, integration.credential_type):
        only = await single_organisation_credential(db, integration.credential_type)
        if only is not None:
            return LoginChoice(only, "organisation", None)
    if conn.department_id in department_ids and holds_copied_login(conn, integration):
        return LoginChoice(None, "legacy", conn)
    raise ConnectionRequired(integration, "not_configured")


async def offer_problem(
    db: AsyncSession,
    *,
    integration: Integration,
    conn: m.McpConnection,
    department_ids: Sequence[uuid.UUID],
    live_member_id: uuid.UUID | None = None,
    agent: m.Agent | None = None,
) -> ConnectionRequired | None:
    """Why the tool may not be offered, or None when a login resolves (steps
    2-5 of the order, without an agent's explicit choice -- a caller that has
    one offers the tool anyway). One rule for every offer, so an offer is
    never accepted and then refused for want of a login. An incomplete login
    still resolves: the call names its missing fields
    (`runtime_env._require_complete`) instead of hiding the tool."""
    try:
        await resolve_login(
            db,
            agent=agent,
            integration=integration,
            conn=conn,
            live_member_id=live_member_id,
            department_ids=department_ids,
            right="read",
        )
    except ConnectionRequired as exc:
        return exc
    return None


async def login_chosen(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID | None,
    department_id: uuid.UUID | None,
    tool_key: str,
    login_row: m.McpConnection | None = None,
) -> bool:
    """Whether a login tool needs no narrowing pin for this agent: it has an
    explicit access row (`AgentToolAccess`, what a login-pinned row became
    when consolidated), or an organisation login of the tool's type is
    enabled for its department. The legacy "set connection_id" rule only
    applies when neither is true."""
    from oc8.connections.integrations import (
        enabled_tool_pack_connection,
        integration_for_connection,
    )

    if agent_id is not None:
        found = await db.scalar(
            select(m.AgentToolAccess.id).where(
                m.AgentToolAccess.agent_id == agent_id, m.AgentToolAccess.tool_key == tool_key
            )
        )
        if found is not None:
            return True
    if department_id is None:
        return False
    template = await enabled_tool_pack_connection(db, tool_key)
    integration = template[2] if template is not None else None
    if integration is None and login_row is not None:
        integration = integration_for_connection(login_row)
    if integration is None or integration.credential_type is None:
        return False
    return bool(
        await enabled_credentials(
            db, credential_type=integration.credential_type, department_id=department_id
        )
    )
