"""The environment one tool call's bridge is launched with, for one run.

Every launch path (MCP gateway, isolated-runtime step endpoint, in-process
engine) calls this instead of resolving the connection's stored config
directly: the run decides whose account the call uses (connections/access.py).
A person's account is never put in a pooled bridge (`reusable=False`), so two
people never share a bridge process.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.mcp_env import (
    OAUTH_DELEGATED_REF_PREFIX,
    has_oauth_ref,
    resolve_mcp_env,
)
from oc8.audit import append_event
from oc8.authz.pdp import ToolPolicy
from oc8.capas.discovery import resolve_tool_pack_connection
from oc8.capas.materialise import unbound_connection
from oc8.connections.access import (
    ConnectionRequired,
    ToolAccess,
    resolve_tool_access,
)
from oc8.connections.acting import resolve_acting_person
from oc8.connections.credential_env import copied_missing, with_login
from oc8.connections.integrations import (
    enabled_tool_pack_connection,
    integration_enabled,
    integration_for_connection,
)
from oc8.connections.login import holds_copied_login, live_member_for, offer_problem
from oc8.connections.tool_connection import tool_connections
from oc8.credentials.registry import CredentialTypeNotFound, get_credential_type
from oc8.credentials.service import missing_required
from oc8.oauth.errors import OAuthReauthRequired
from oc8.runtime.run_context import merge_context

#: Told to a bridge when its token is one person's own sign-in (not an app's).
OAUTH_GRANT_ENV: Final = "OC8_OAUTH_GRANT"
OAUTH_ACCOUNT_ENV: Final = "OC8_OAUTH_ACCOUNT"


@dataclass(frozen=True)
class McpLaunchEnv:
    env: dict[str, str]
    reusable: bool
    #: The right the bridge's account was resolved for (`ToolAccess.right`):
    #: a "read" bridge must refuse every call that needs more.
    right: Literal["read", "modify"] = "modify"


def _cfg(conn: m.McpConnection) -> dict[str, Any]:
    return conn.config if isinstance(conn.config, dict) else {}


def _without_identity(conn: m.McpConnection) -> dict[str, Any]:
    """The config a bridge that only LISTS tools starts with when the run has
    no account: an allowlist, not a filter. Only the plain `env` the plugin's
    own manifest declares for this connection -- never anything stored on the
    row (a department's login, a secret or OAuth reference, wherever it sits),
    and no `secret_env` at all."""
    cfg = _cfg(conn)
    declared = resolve_tool_pack_connection(
        str(cfg.get("_plugin_name", "")), str(cfg.get("_connection_key", ""))
    )
    env = dict(declared.config.get("env") or {}) if declared is not None else {}
    return {
        "env": {str(k): v for k, v in env.items() if isinstance(v, str)},
        "secret_env": {},
    }


async def require_enabled(db: AsyncSession, conn: m.McpConnection) -> None:
    """Refuse a connectable integration whose plugin is not enabled in this
    tenant -- before any environment is built or any token minted. Disabling
    keeps the plugin's rows (there is no uninstall)."""
    integration = integration_for_connection(conn)
    if integration is not None and not await integration_enabled(db, integration):
        raise ConnectionRequired(integration, "not_configured")


async def _effective(
    db: AsyncSession, cfg: dict[str, Any], access: ToolAccess, tenant_id: Any
) -> dict[str, Any]:
    if access.kind == "legacy":
        return cfg
    out = dict(cfg)
    env = dict(cfg.get("env") or {})
    secret_env = dict(cfg.get("secret_env") or {})
    if access.kind == "oauth":
        assert access.oauth_connection is not None
        out["oauth_connection_id"] = str(access.oauth_connection.id)
        if access.oauth_connection.grant_type == "authorization_code":
            # One person's own account: the organisation's delegated mailboxes
            # (a service account's identities) do not belong in this bridge.
            secret_env = {
                k: v
                for k, v in secret_env.items()
                if not str(v).startswith(OAUTH_DELEGATED_REF_PREFIX)
            }
            env[OAUTH_GRANT_ENV] = "authorization_code"
            env[OAUTH_ACCOUNT_ENV] = access.oauth_connection.account_label
    else:
        assert access.credential is not None and access.integration is not None
        cred_type = await get_credential_type(
            db, tenant_id=tenant_id, name=access.credential.credential_type
        )
        # The chosen login replaces the stored identity whole (with_login).
        chosen = with_login(
            {"env": env, "secret_env": secret_env},
            access.credential,
            cred_type,
            access.integration.credential_env_fields,
        )
        env, secret_env = chosen["env"], chosen["secret_env"]
    out["env"] = env
    out["secret_env"] = secret_env
    return out


async def _require_complete(
    db: AsyncSession, access: ToolAccess, tenant_id: Any, conn: m.McpConnection | None = None
) -> None:
    """Refuse a chosen login that leaves a required field empty -- before any
    bridge starts -- naming the fields. A department's own copied login
    (`legacy`, unconsolidated) is checked the same way."""
    integration = access.integration
    if (
        access.kind == "legacy"
        and conn is not None
        and integration is not None
        and integration.credential_type
        and holds_copied_login(conn, integration)
    ):
        try:
            copied_type = await get_credential_type(
                db, tenant_id=tenant_id, name=integration.credential_type
            )
        except CredentialTypeNotFound:
            return
        missing = copied_missing(_cfg(conn), copied_type, integration.credential_env_fields)
        if missing:
            raise ConnectionRequired(integration, "incomplete", missing=missing)
        return
    if access.kind != "credential" or access.credential is None or access.integration is None:
        return
    cred = access.credential
    cred_type = await get_credential_type(db, tenant_id=tenant_id, name=cred.credential_type)
    missing = missing_required(cred_type, cred.field_values or {}, cred.secret_refs or {})
    if missing:
        raise ConnectionRequired(access.integration, "incomplete", missing=missing)


#: Run context key: tools a listing skipped for an admin's configuration
#: error (`ConnectionRequired.as_notice`). Non-blocking, unlike
#: `connection_required`; the chat shows it as a quiet line.
TOOL_NOTICES = "tool_notices"


def _notices(context: Any) -> list[dict[str, Any]]:
    raw = context.get(TOOL_NOTICES) if isinstance(context, dict) else None
    return [n for n in raw if isinstance(n, dict)] if isinstance(raw, list) else []


async def record_tool_notice(
    db: AsyncSession,
    run: m.AgentRun,
    exc: ConnectionRequired,
    *,
    tool: str,
    ctx: dict[str, Any] | None = None,
) -> None:
    """Record (once per tool) why a listing skipped `tool`."""
    notice = exc.as_notice(tool)
    current = _notices(run.context)
    if notice in current:
        return
    notices = [n for n in current if n.get("tool") != tool] + [notice]
    await merge_context(db, run, {TOOL_NOTICES: notices})
    if ctx is not None:
        ctx[TOOL_NOTICES] = [n for n in _notices(ctx) if n.get("tool") != tool] + [notice]


async def _clear_tool_notice(
    db: AsyncSession, run: m.AgentRun, tool: str, ctx: dict[str, Any] | None
) -> None:
    """The tool works now: its notice is stale."""
    current = _notices(run.context)
    if any(n.get("tool") == tool for n in current):
        await merge_context(db, run, {TOOL_NOTICES: [n for n in current if n.get("tool") != tool]})
    if ctx is not None and any(n.get("tool") == tool for n in _notices(ctx)):
        ctx[TOOL_NOTICES] = [n for n in _notices(ctx) if n.get("tool") != tool]


async def _clear_connection_required(
    db: AsyncSession, run: m.AgentRun, integration: str, ctx: dict[str, Any] | None
) -> None:
    """This integration works now (the person reconnected): its call to
    action is stale. Another integration's stays."""

    def _is_ours(context: Any) -> bool:
        cta = context.get("connection_required") if isinstance(context, dict) else None
        return isinstance(cta, dict) and cta.get("integration") == integration

    if _is_ours(run.context):
        await merge_context(db, run, {"connection_required": None})
    if ctx is not None and _is_ours(ctx):
        ctx["connection_required"] = None


def _connection_id(access: ToolAccess, cfg: dict[str, Any]) -> str | None:
    if access.oauth_connection is not None:
        return str(access.oauth_connection.id)
    if access.credential is not None:
        return str(access.credential.id)
    raw = cfg.get("oauth_connection_id")
    return str(raw) if raw else None


async def run_mcp_env(
    db: AsyncSession,
    *,
    run: m.AgentRun,
    conn: m.McpConnection,
    tool: str | None = None,
    listing: bool = False,
    ctx: dict[str, Any] | None = None,
    right: Literal["read", "modify"] | None = None,
    record: bool = True,
) -> McpLaunchEnv:
    """`tool`: the call this launch serves (audited). `listing`: only listing
    tools -- without a sign-in account the bridge starts without it, so the
    tools stay visible and the first real call explains what is missing; a
    login integration without a usable login starts no bridge: the reason is
    raised (the caller skips the tool) but not recorded on the run -- only an
    actual call asks for a connection. `right`: the right the call needs, when
    `tool` does not say (a core tool reading through the connection). An
    incomplete or ambiguous login at a listing is recorded as a notice
    (`tool_notices`), not a connect prompt. `ctx`: a
    caller's in-request copy of the run context that it writes back whole
    (the isolated-runtime step endpoint); the CTA is merged into it too.
    `record=False`: raise a missing account without putting its call to
    action on the run -- for a session opened before any tool is chosen, so a
    connection the run never uses asks for nothing (E2E N3)."""
    cfg = _cfg(conn)
    try:
        await require_enabled(db, conn)
    except ConnectionRequired as exc:
        if not listing and record:
            patch = {"connection_required": exc.as_context()}
            await merge_context(db, run, patch)
            if ctx is not None:
                ctx.update(patch)
        raise
    agent = await db.get(m.Agent, run.agent_id)
    if agent is None:
        env = await resolve_mcp_env(db, tenant_id=run.tenant_id, cfg=cfg, connection_name=conn.name)
        return McpLaunchEnv(env, reusable=not has_oauth_ref(cfg))
    try:
        access = await resolve_tool_access(
            db, run=run, agent=agent, conn=conn, tool=tool, right=right
        )
        await _require_complete(db, access, run.tenant_id, conn)
    except ConnectionRequired as exc:
        if listing and exc.integration.kind == "credential":
            # A login integration's bridge cannot start without its login (it
            # would only report "Connection closed"): the tool is skipped. No
            # connect prompt -- an agent that merely has the tool enabled
            # must not ask for a connection on every run. An admin's
            # configuration error (incomplete, ambiguous) is recorded as a
            # quiet notice, so the tool does not vanish without a word.
            if exc.is_notice:
                await record_tool_notice(db, run, exc, tool=conn.name, ctx=ctx)
            raise
        if listing:
            # A sign-in integration's tools stay listed; the first call shows
            # the call to action.
            env = await resolve_mcp_env(
                db,
                tenant_id=run.tenant_id,
                cfg=_without_identity(conn),
                connection_name=conn.name,
            )
            return McpLaunchEnv(env, reusable=False, right="read")
        if record:
            patch = {"connection_required": exc.as_context()}
            await merge_context(db, run, patch)
            if ctx is not None:
                ctx.update(patch)
        raise
    effective = await _effective(db, cfg, access, run.tenant_id)
    try:
        # A personal OAuth connection is minted only here, after
        # resolve_tool_access chose it for this run -- never from a stored config.
        env = await resolve_mcp_env(
            db,
            tenant_id=run.tenant_id,
            cfg=effective,
            connection_name=conn.name,
            allow_personal=access.kind == "oauth",
        )
    except OAuthReauthRequired:
        # The refresh failed (get_access_token already marked the row
        # needs_reauth): the person must reconnect -- say so like any other
        # missing account instead of a raw provider error.
        if access.kind != "oauth" or access.integration is None:
            raise
        required = ConnectionRequired(access.integration, "needs_reauth")
        if listing:
            env = await resolve_mcp_env(
                db,
                tenant_id=run.tenant_id,
                cfg=_without_identity(conn),
                connection_name=conn.name,
            )
            return McpLaunchEnv(env, reusable=False, right="read")
        if record:
            patch = {"connection_required": required.as_context()}
            await merge_context(db, run, patch)
            if ctx is not None:
                ctx.update(patch)
        raise required from None
    if not listing and access.integration is not None:
        await _clear_connection_required(db, run, access.integration.name, ctx)
    if access.integration is not None:
        await _clear_tool_notice(db, run, conn.name, ctx)
    if tool is not None:
        await append_event(
            db,
            tenant_id=run.tenant_id,
            actor_type="agent",
            actor_id=run.agent_id,
            category="tool_action",
            action="connection.used",
            resource={
                "tool": tool,
                "connection": conn.name,
                "access": access.kind,
                "connection_id": _connection_id(access, cfg),
                "on_behalf_of": str(access.on_behalf_of) if access.on_behalf_of else None,
                "run_id": str(run.id),
            },
        )
    return McpLaunchEnv(
        env,
        reusable=access.kind == "legacy" and not has_oauth_ref(effective),
        right=access.right,
    )


async def listing_notice(
    db: AsyncSession, *, run: m.AgentRun, conn: m.McpConnection
) -> ConnectionRequired | None:
    """The configuration notice a listing of `conn` would record (an
    incomplete or ambiguous login), recorded now -- without starting a
    bridge or minting a token. None when the login resolves, or when what
    is missing is nobody's configuration error (no login at all)."""
    integration = integration_for_connection(conn)
    if integration is None or integration.kind != "credential":
        return None
    agent = await db.get(m.Agent, run.agent_id)
    if agent is None:
        return None
    try:
        await require_enabled(db, conn)
        access = await resolve_tool_access(db, run=run, agent=agent, conn=conn)
        await _require_complete(db, access, run.tenant_id, conn)
    except ConnectionRequired as exc:
        if exc.is_notice:
            await record_tool_notice(db, run, exc, tool=conn.name)
            return exc
    return None


async def integration_connections(
    db: AsyncSession,
    *,
    agent: m.Agent,
    policies: Mapping[str, ToolPolicy],
    have: set[str],
    run: m.AgentRun | None = None,
) -> list[m.McpConnection]:
    """The connection of each enabled tool the run has no system for yet --
    when it can act at all: an OAuth integration (user mode), a tool with an
    access row (the agent's explicit choice), or a login type for which a
    login resolves (`connections.login.offer_problem`: the person's own in a
    live chat, one enabled for the agent's department, the organisation's
    single one). Several logins and none chosen (`ambiguous`) still joins:
    its listing records the notice naming it (`tool_notices`) instead of the
    tool vanishing. The frame and narrowing (`policies`) stay the ceiling:
    they, not the row's department, decide whether the agent reaches the tool.

    Which row carries the server config: the tool's one row
    (`tool_connection`; the agent's own department's while unconsolidated),
    else one materialised now from the enabled plugin's manifest. Another
    department's row is never used: it carries that department's identity."""
    wanted = sorted(key for key, policy in policies.items() if policy.enabled and key not in have)
    if not wanted:
        return []
    found = {
        conn.name: conn
        for conn in await tool_connections(
            db, names=wanted, department_id=agent.department_id, connected_only=False
        )
    }
    access_keys = set(
        (
            await db.execute(
                select(m.AgentToolAccess.tool_key).where(m.AgentToolAccess.agent_id == agent.id)
            )
        )
        .scalars()
        .all()
    )
    live_member: uuid.UUID | None = None
    if run is not None:
        live_member = await live_member_for(db, run, await resolve_acting_person(db, run))
    out: list[m.McpConnection] = []
    for name in wanted:
        conn = found.get(name)
        template = None
        if conn is not None:
            integration = integration_for_connection(conn)
        else:
            template = await enabled_tool_pack_connection(db, name)
            integration = template[2] if template is not None else None
        if integration is None:
            continue
        if conn is None:
            assert template is not None
            manifest, pack_conn, _ = template
            candidate = unbound_connection(
                tenant_id=agent.tenant_id, manifest=manifest, conn=pack_conn
            )
        else:
            candidate = conn
        # The builder and the Copilot offer exactly what passes here
        # (connections.usable).
        if name not in access_keys and integration.kind != "oauth":
            problem = await offer_problem(
                db,
                integration=integration,
                conn=candidate,
                department_ids=[agent.department_id] if agent.department_id else [],
                live_member_id=live_member,
                agent=agent,
            )
            if problem is not None and not problem.is_notice:
                continue
        if conn is None:
            db.add(candidate)
            await db.flush()
        out.append(candidate)
    return out
