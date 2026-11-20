"""Teams and Contacts tools -- Teams is READ-ONLY here, deliberately.

Microsoft Graph has no application permission for *sending* a Teams message.
`POST /teams/{id}/channels/{id}/messages` and `POST /chats/{id}/messages` are
covered only by the delegated-only `ChannelMessage.Send` / `Chat.ReadWrite`
scopes; the sole app-only permission in that area, `Teamwork.Migrate.All`, is
for one-time migration/import of historical messages, not for an agent talking
to people. This plugin authenticates org-wide with `client_credentials` (design
doc §4.1a: there is no signed-in anyone), so a send tool here could only ever
403 -- every call, in every tenant, no matter what an admin consents to.

So the two send tools this pack originally listed were removed rather than
shipped as dead surface -- the same "state the Graph asymmetry, don't oversell
it" discipline as `powerpoint.py`'s missing slide-level edit. Reading
stays: `teams_list_channels`, `teams_list_chats` and the Contacts tools all
have real application permissions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import Tool

from .. import graph
from .._user import USER_ID_PROPERTY, user_base

#: Appended to the three user-scoped descriptions below. The channel listing
#: tool is NOT user-scoped -- it addresses a team id and never went through
#: `/me` in the first place.
_USER_SCOPED = (
    " Acts on the chat list / contacts of the user named by `userId`, or the "
    "connection's configured default mailbox when `userId` is omitted."
)


async def teams_list_channels(args: dict[str, Any]) -> Any:
    result = await graph.get(f"/teams/{args['teamId']}/channels")
    return result.get("value", [])


async def teams_list_chats(args: dict[str, Any]) -> Any:
    base = user_base(args)
    result = await graph.get(f"{base}/chats")
    return result.get("value", [])


async def contacts_search(args: dict[str, Any]) -> Any:
    base = user_base(args)
    result = await graph.get(f"{base}/contacts", params={"$search": f'"{args["query"]}"'})
    return result.get("value", [])


async def contacts_get(args: dict[str, Any]) -> Any:
    base = user_base(args)
    return await graph.get(f"{base}/contacts/{args['contactId']}")


TOOLS: list[Tool] = [
    Tool(
        name="teams_list_channels",
        description="List a team's channels.",
        inputSchema={
            "type": "object",
            "properties": {"teamId": {"type": "string"}},
            "required": ["teamId"],
        },
    ),
    Tool(
        name="teams_list_chats",
        description="List a user's chats." + _USER_SCOPED,
        inputSchema={"type": "object", "properties": {**USER_ID_PROPERTY}},
    ),
    Tool(
        name="contacts_search",
        description="Search contacts." + _USER_SCOPED,
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}, **USER_ID_PROPERTY},
            "required": ["query"],
        },
    ),
    Tool(
        name="contacts_get",
        description="Get one contact by id." + _USER_SCOPED,
        inputSchema={
            "type": "object",
            "properties": {"contactId": {"type": "string"}, **USER_ID_PROPERTY},
            "required": ["contactId"],
        },
    ),
]

CALL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
    "teams_list_channels": teams_list_channels,
    "teams_list_chats": teams_list_chats,
    "contacts_search": contacts_search,
    "contacts_get": contacts_get,
}
