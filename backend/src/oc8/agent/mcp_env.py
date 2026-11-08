"""The environment an MCP bridge subprocess is launched with.

FOUR places launch a bridge: the in-process engine loop (agent/engine.py), the
MCP gateway a packaged runtime talks to (api/mcp_gateway.py), the internal-agent
step endpoint (api/v1/internal_agent.py), and the "test connection" button
(api/v1/mcp.py). Each used to carry its own copy of the same three-line loop
over `secret_env`, which is precisely how one path gains a capability the other
three silently miss. One function now, called by all four.

`secret_env` maps an environment-variable NAME to a ref, resolved only
immediately before the launch -- never when the connection is created, and never
on the way out to a browser. Two kinds of ref exist:

* a plain secret-store name, resolved verbatim (odoo_mcp's Odoo password);
* ``oauth:<label>``, meaning "mint a FRESH provider access token from the OAuth
  connection this MCP connection was set up against". Only the prefix is read;
  the label after it is for a human reading the manifest. A Microsoft Graph
  access token lives about an hour, so a stored copy would be expired on nearly
  every launch -- the token has to be minted per launch, from the long-lived
  credential the OAuthConnection holds. `config["oauth_connection_id"]` names
  that connection and is written by the plugin setup form (api/v1/capas.py)
  from the manifest's `[plugin.setup.oauth_provision]` block;
* ``oauth-delegated:<mailbox>``, Google Workspace's domain-wide-delegation
  counterpart to ``oauth:`` -- the mailbox to impersonate travels inside the
  ref itself rather than through a separate lookup table. Resolved by minting
  a token for that mailbox from the same `oauth_connection_id`, scoped with
  `config["delegated_scope"]` (a plain, non-secret string a plugin's setup
  writes from its manifest -- never hardcoded here).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.oauth.tokens import get_access_token, mint_delegated_token
from oc8.secrets.service import resolve_secret

logger = logging.getLogger(__name__)

#: Marks a `secret_env` ref as "mint this fresh at launch", not "look this up".
OAUTH_REF_PREFIX = "oauth:"

#: Marks a `secret_env` ref as "mint a per-mailbox delegated token", the
#: domain-wide-delegation counterpart to `OAUTH_REF_PREFIX`'s single identity.
#: The mailbox to impersonate travels inside the ref itself
#: (`oauth-delegated:<mailbox>`) rather than through a separate lookup table.
OAUTH_DELEGATED_REF_PREFIX = "oauth-delegated:"


def has_oauth_ref(cfg: dict[str, Any]) -> bool:
    """Does this connection's environment expire?

    Asked by anything that would otherwise HOLD a launched bridge open: the
    environment is captured once, at launch, and a minted token may have as
    little as `EXPIRY_SKEW_SECONDS` of life left when it is handed over. The
    one place that knows what an `oauth:` ref means is this module, so callers
    ask here rather than re-testing the prefix themselves.
    """
    return any(
        str(ref).startswith(OAUTH_REF_PREFIX) or str(ref).startswith(OAUTH_DELEGATED_REF_PREFIX)
        for ref in dict(cfg.get("secret_env", {})).values()
    )


async def resolve_mcp_env(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    cfg: dict[str, Any],
    connection_name: str = "",
) -> dict[str, str]:
    """Plain `env` plus every `secret_env` entry resolved to a real value.

    ONE delegated identity failing to mint degrades ONE identity, never the
    launch. A `reusable=False` connection (any oauth-* ref -- see
    `has_oauth_ref`) re-runs this per tool call and mints for EVERY configured
    delegated identity, including on a call that touches none of them. Letting
    a single failed mint propagate meant the day one mailbox in the list was
    offboarded, deleted or misspelled, the bridge stopped launching and the
    whole pack went dark -- including every tool that never touches a mailbox
    at all. So a failed delegated mint leaves that one env var EMPTY and is
    logged; the bridge's own per-identity lookup (google_api.py's
    `resolve_mailbox_token`) then raises a precise "no token available for
    mailbox X" at the one tool call that actually needed X, and nothing else
    is affected.

    The self `oauth:` token is NOT treated this way: it is the identity
    everything non-delegated authenticates as, so a bridge launched without it
    has no working tools to degrade to and should fail loudly here instead.
    """
    env = {str(name): str(value) for name, value in dict(cfg.get("env", {})).items()}
    for env_name, secret_ref in dict(cfg.get("secret_env", {})).items():
        ref = str(secret_ref)
        if ref.startswith(OAUTH_DELEGATED_REF_PREFIX):
            subject = ref[len(OAUTH_DELEGATED_REF_PREFIX) :]
            connection_id = _require_oauth_connection_id(
                cfg, env_name=str(env_name), connection_name=connection_name
            )
            try:
                await _refuse_personal_connection(
                    db, tenant_id=tenant_id, connection_id=connection_id
                )
                env[str(env_name)] = await mint_delegated_token(
                    db,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    subject=subject,
                    scope=str(cfg.get("delegated_scope", "")),
                )
            except Exception as exc:  # degrade one identity, never the whole launch
                logger.warning(
                    "MCP connection %r: could not mint a delegated token for %r (%s) -- "
                    "%s is being launched empty, so only tools acting as that identity "
                    "will fail; every other tool in this pack keeps working",
                    connection_name or "<unnamed>",
                    subject,
                    exc,
                    env_name,
                )
                env[str(env_name)] = ""
        elif ref.startswith(OAUTH_REF_PREFIX):
            env[str(env_name)] = await _mint_oauth_token(
                db,
                tenant_id=tenant_id,
                cfg=cfg,
                env_name=str(env_name),
                connection_name=connection_name,
            )
        else:
            env[str(env_name)] = await resolve_secret(db, tenant_id=tenant_id, ref=ref)
    return env


async def _refuse_personal_connection(
    db: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> None:
    """A plugin's MCP configuration is the organisation's: it may be fed by an
    organisation connection only, never by somebody's own account."""
    conn = await db.get(m.OAuthConnection, connection_id)
    if conn is not None and conn.tenant_id == tenant_id and conn.owner_member_id is not None:
        raise RuntimeError(
            f"oauth_connection_id {connection_id} is a personal connection; "
            "an MCP connection can only use an organisation connection"
        )


def _require_oauth_connection_id(
    cfg: dict[str, Any], *, env_name: str, connection_name: str
) -> uuid.UUID:
    raw_id = cfg.get("oauth_connection_id")
    if not raw_id:
        raise RuntimeError(
            f"MCP connection {connection_name or '<unnamed>'!r} declares an OAuth-backed "
            f"secret {env_name!r} but has no oauth_connection_id -- finish this plugin's "
            f"setup form before running an agent against it"
        )
    try:
        return uuid.UUID(str(raw_id))
    except ValueError as exc:
        raise RuntimeError(
            f"MCP connection {connection_name or '<unnamed>'!r} has an unusable "
            f"oauth_connection_id {raw_id!r}"
        ) from exc


async def _mint_oauth_token(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    cfg: dict[str, Any],
    env_name: str,
    connection_name: str,
) -> str:
    connection_id = _require_oauth_connection_id(
        cfg, env_name=env_name, connection_name=connection_name
    )
    await _refuse_personal_connection(db, tenant_id=tenant_id, connection_id=connection_id)
    return await get_access_token(db, tenant_id=tenant_id, connection_id=connection_id)
