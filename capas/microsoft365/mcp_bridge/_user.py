"""Which mailbox/calendar/chat-list a tool call acts on.

This connection authenticates app-only (`client_credentials`, see
`oauth/provisioning.py`), so the token carries no user at all. Microsoft Graph
answers every `/me/...` path with

    400 BadRequest -- /me request is only valid with delegated authentication flow.

because `/me` resolves through the token's user claim and an app-only token has
none. There is no signed-in anyone here: every mailbox-scoped call must name
its target explicitly as `/users/{id}/...`.

Two ways to name it, in this order:

1. the tool call's own optional `userId` argument -- a UPN
   (`sina@contoso.com`) or a Graph object id -- which is what lets one agent
   work across several mailboxes the app registration is scoped to; or
2. the connection's configured default mailbox, carried into the bridge
   process as `MICROSOFT365_DEFAULT_USER` from the setup form's `default_user`
   field, so the common single-mailbox case does not force the model to guess
   a UPN on every single call.

A person's own (delegated) token -- core sets `OC8_OAUTH_GRANT=authorization_code`
-- does have a user: such a call works on `/me/...`, and only on the person's own
mailbox. A `userId` naming anyone else is refused, never forwarded to Graph.

Neither present is a configuration error, not a call to make against Graph:
raising here says so in words an operator can act on, rather than building
`/users//messages` and letting Graph answer something unrelated.
"""

from __future__ import annotations

import os
from typing import Any

#: Set by the core from the setup form's `default_user` field -- see
#: plugin.toml's `[plugin.setup.mcp.env_fields]`.
DEFAULT_USER_ENV = "MICROSOFT365_DEFAULT_USER"

#: The `userId` input-schema property, identical on all 19 mailbox-scoped
#: tools. Declared once so the wording cannot drift between them; NEVER added
#: to a tool's `required` list -- omitting it means "use the default mailbox".
USER_ID_PROPERTY: dict[str, Any] = {
    "userId": {
        "type": "string",
        "description": (
            "UPN (e.g. sina@contoso.com) or Graph object id of the mailbox, "
            "calendar or chat list to act on. Omit to use the connection's "
            "configured default mailbox."
        ),
    }
}


def resolve_user(args: dict[str, Any]) -> str:
    """The target user for this call, or a RuntimeError naming both ways to fix it."""
    explicit = str(args.get("userId") or "").strip()
    if explicit:
        return explicit
    default = os.environ.get(DEFAULT_USER_ENV, "").strip()
    if default:
        return default
    raise RuntimeError(
        "no userId given and no default_user configured for this connection — "
        "pass userId (a UPN or Graph object id) or set a default mailbox in the "
        "plugin's setup form"
    )


#: Set by the core when the token is one person's own sign-in.
GRANT_ENV = "OC8_OAUTH_GRANT"
#: The connected account's label (the person's sign-in name).
ACCOUNT_ENV = "OC8_OAUTH_ACCOUNT"


def is_personal() -> bool:
    return os.environ.get(GRANT_ENV, "") == "authorization_code"


def user_base(args: dict[str, Any]) -> str:
    """The Graph path prefix for this call: `/me` for a person's own token
    (a `userId` naming anyone but that person is refused), `/users/{id}`
    otherwise."""
    if is_personal():
        explicit = str(args.get("userId") or "").strip()
        own = os.environ.get(ACCOUNT_ENV, "").strip()
        if explicit and explicit.casefold() != own.casefold():
            who = own or "its connected person"
            raise RuntimeError(f"this connection acts as {who} only; omit userId")
        return "/me"
    return f"/users/{resolve_user(args)}"
