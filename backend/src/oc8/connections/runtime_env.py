"""The environment one tool call's bridge is launched with, for one run.

Every launch path (MCP gateway, isolated-runtime step endpoint, in-process
engine) calls this instead of resolving the connection's stored config
directly: the run decides whose account the call uses (connections/access.py).
A person's account is never put in a pooled bridge (`reusable=False`), so two
people never share a bridge process.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

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
from oc8.connections.credential_env import credential_env_targets, credential_mcp_env
from oc8.connections.integrations import (
    enabled_tool_pack_connection,
    integration_enabled,
    integration_for_connection,
)
from oc8.connections.usable import organisation_access_ready
from oc8.credentials.registry import get_credential_type
from oc8.oauth.errors import OAuthReauthRequired
from oc8.runtime.run_context import merge_context

#: Told to a bridge when its token is one person's own sign-in (not an app's).
OAUTH_GRANT_ENV: Final = "OC8_OAUTH_GRANT"
OAUTH_ACCOUNT_ENV: Final = "OC8_OAUTH_ACCOUNT"


@dataclass(frozen=True)
class McpLaunchEnv:
    env: dict[str, str]
    reusable: bool


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
        fields = access.integration.credential_env_fields
        # The chosen login replaces the stored identity whole: a variable it
        # leaves empty must not keep the department's value (never a mix of
        # two logins in one bridge).
        for target in credential_env_targets(cred_type, fields):
            env.pop(target, None)
            secret_env.pop(target, None)
        cred_env, cred_secret_env = credential_mcp_env(access.credential, cred_type, fields)
        env.update(cred_env)
        secret_env.update(cred_secret_env)
    out["env"] = env
    out["secret_env"] = secret_env
    return out


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
) -> McpLaunchEnv:
    """`tool`: the call this launch serves (audited). `listing`: only listing
    tools -- without an account the bridge starts without it, so the tools
    stay visible and the first real call explains what is missing. `ctx`: a
    caller's in-request copy of the run context that it writes back whole
    (the isolated-runtime step endpoint); the CTA is merged into it too."""
    cfg = _cfg(conn)
    try:
        await require_enabled(db, conn)
    except ConnectionRequired as exc:
        if not listing:
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
        access = await resolve_tool_access(db, run=run, agent=agent, conn=conn)
    except ConnectionRequired as exc:
        if listing:
            env = await resolve_mcp_env(
                db,
                tenant_id=run.tenant_id,
                cfg=_without_identity(conn),
                connection_name=conn.name,
            )
            return McpLaunchEnv(env, reusable=False)
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
            return McpLaunchEnv(env, reusable=False)
        patch = {"connection_required": required.as_context()}
        await merge_context(db, run, patch)
        if ctx is not None:
            ctx.update(patch)
        raise required from None
    if not listing and access.integration is not None:
        await _clear_connection_required(db, run, access.integration.name, ctx)
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
    return McpLaunchEnv(env, reusable=access.kind == "legacy" and not has_oauth_ref(effective))


async def integration_connections(
    db: AsyncSession, *, agent: m.Agent, policies: Mapping[str, ToolPolicy], have: set[str]
) -> list[m.McpConnection]:
    """The connection of each enabled tool the run has no system for yet --
    when it can act at all: an OAuth integration (user mode), a tool with an
    access row, or a login type with exactly one organisation credential. The
    frame and narrowing (`policies`) stay the ceiling.

    Which row carries the server config: the agent's own department's, else
    the tenant's department-less one, else one materialised now from the
    enabled plugin's manifest. A department's setup form adopts the
    department-less row, so without the last step every other department
    would lose the tool. Another department's row is never used: it carries
    that department's identity."""
    wanted = sorted(key for key, policy in policies.items() if policy.enabled and key not in have)
    if not wanted:
        return []
    rows = (
        (
            await db.execute(
                select(m.McpConnection)
                .where(
                    m.McpConnection.name.in_(wanted),
                    (m.McpConnection.department_id == agent.department_id)
                    | (
                        m.McpConnection.department_id.is_(None)
                        & m.McpConnection.credential_id.is_(None)
                    ),
                )
                .order_by(m.McpConnection.created_at)
            )
        )
        .scalars()
        .all()
    )
    own: dict[str, m.McpConnection] = {}
    unbound: dict[str, m.McpConnection] = {}
    for row in rows:
        (own if row.department_id is not None else unbound).setdefault(row.name, row)
    access_keys = set(
        (
            await db.execute(
                select(m.AgentToolAccess.tool_key).where(m.AgentToolAccess.agent_id == agent.id)
            )
        )
        .scalars()
        .all()
    )
    out: list[m.McpConnection] = []
    for name in wanted:
        conn = own.get(name) or unbound.get(name)
        template = None
        if conn is not None:
            integration = integration_for_connection(conn)
        else:
            template = await enabled_tool_pack_connection(db, name)
            integration = template[2] if template is not None else None
        if integration is None:
            continue
        # The builder offers exactly what passes here (connections.usable).
        if not (name in access_keys or await organisation_access_ready(db, integration)):
            continue
        if conn is None:
            assert template is not None
            manifest, pack_conn, _ = template
            conn = unbound_connection(tenant_id=agent.tenant_id, manifest=manifest, conn=pack_conn)
            db.add(conn)
            await db.flush()
        out.append(conn)
    return out
