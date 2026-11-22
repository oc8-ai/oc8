"""Outlook Mail tools (design doc §4.2). Each handler returns plain JSON-
serializable data -- the MCP server layer (`__main__.py`, Task 13) wraps it
into the SDK's own `types.TextContent`, not this module's job.

Every path is built by `_user.user_base`: `/users/{id}/...` for an app-only
token, `/me/...` for a person's own. `_user.py` says why.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from mcp.types import Tool

from .. import graph
from .._user import USER_ID_PROPERTY, user_base
from ..files import ATTACHMENTS_SCHEMA, FileContent, attachment_files


def _segment(value: object, label: str) -> str:
    """A Graph id as one path segment, whatever characters it carries."""
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"`{label}` is required.")
    return quote(value, safe="")


def _graph_attachments(args: dict[str, Any]) -> list[dict[str, Any]]:
    """The run files to attach, as Graph's inline fileAttachments."""
    return [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": name,
            "contentType": mime,
            "contentBytes": base64.b64encode(data).decode(),
        }
        for name, mime, data in attachment_files(args.get("attachments"))
    ]


async def mail_search(args: dict[str, Any]) -> Any:
    base = user_base(args)
    result = await graph.get(f"{base}/messages", params={"$search": f'"{args["query"]}"'})
    return result.get("value", [])


async def mail_get(args: dict[str, Any]) -> Any:
    base = user_base(args)
    message = _segment(args.get("messageId"), "messageId")
    return await graph.get(f"{base}/messages/{message}")


async def mail_send(args: dict[str, Any]) -> Any:
    files = _graph_attachments(args)
    base = user_base(args)
    message: dict[str, Any] = {
        "subject": args["subject"],
        "body": {"contentType": "Text", "content": args["body"]},
        "toRecipients": [{"emailAddress": {"address": addr}} for addr in args["to"]],
    }
    if files:
        message["attachments"] = files
    await graph.post(f"{base}/sendMail", {"message": message})
    return {"status": "sent"}


async def mail_reply(args: dict[str, Any]) -> Any:
    files = _graph_attachments(args)
    base = user_base(args)
    message = _segment(args.get("messageId"), "messageId")
    body: dict[str, Any] = {"comment": args["body"]}
    if files:
        body["message"] = {"attachments": files}
    await graph.post(f"{base}/messages/{message}/reply", body)
    return {"status": "sent"}


async def mail_create_draft(args: dict[str, Any]) -> Any:
    files = _graph_attachments(args)
    base = user_base(args)
    draft: dict[str, Any] = {
        "subject": args["subject"],
        "body": {"contentType": "Text", "content": args["body"]},
        "toRecipients": [{"emailAddress": {"address": addr}} for addr in args.get("to", [])],
    }
    if files:
        draft["attachments"] = files
    return await graph.post(f"{base}/messages", draft)


async def mail_list_attachments(args: dict[str, Any]) -> Any:
    base = user_base(args)
    message = _segment(args.get("messageId"), "messageId")
    result = await graph.get(
        f"{base}/messages/{message}/attachments",
        params={"$select": "id,name,contentType,size,isInline"},
    )
    return [
        {key: item.get(key) for key in ("id", "name", "contentType", "size", "isInline")}
        for item in result.get("value", [])
    ]


async def mail_get_attachment(args: dict[str, Any]) -> Any:
    """One attachment, as a file: core keeps it as a file of the run."""
    base = user_base(args)
    message = _segment(args.get("messageId"), "messageId")
    attachment = _segment(args.get("attachmentId"), "attachmentId")
    found = await graph.get(f"{base}/messages/{message}/attachments/{attachment}")
    if found.get("@odata.type") != "#microsoft.graph.fileAttachment":
        raise RuntimeError(
            "This attachment is not a file (an attached mail, event or link) and cannot be "
            "fetched as one."
        )
    data = base64.b64decode(str(found.get("contentBytes") or ""))
    name = str(found.get("name") or "attachment")
    mime = str(found.get("contentType") or "application/octet-stream")
    return FileContent(
        name=name,
        mime_type=mime,
        data=data,
        details={"id": found.get("id"), "name": name, "contentType": mime, "size": len(data)},
    )


async def mail_move(args: dict[str, Any]) -> Any:
    base = user_base(args)
    message = _segment(args.get("messageId"), "messageId")
    return await graph.post(
        f"{base}/messages/{message}/move",
        {"destinationId": args["destinationFolderId"]},
    )


async def mail_delete(args: dict[str, Any]) -> Any:
    base = user_base(args)
    message = _segment(args.get("messageId"), "messageId")
    await graph.delete(f"{base}/messages/{message}")
    return {"status": "deleted"}


async def mail_list_folders(args: dict[str, Any]) -> Any:
    base = user_base(args)
    result = await graph.get(f"{base}/mailFolders")
    return result.get("value", [])


async def mail_mark_read(args: dict[str, Any]) -> Any:
    base = user_base(args)
    message = _segment(args.get("messageId"), "messageId")
    return await graph.patch(f"{base}/messages/{message}", {"isRead": args.get("isRead", True)})


#: Appended to every description below, so the addressing rule is stated on the
#: surface the model actually reads rather than only in this file's docstring.
_MAILBOX = (
    " Acts on the mailbox named by `userId`, or the connection's configured "
    "default mailbox when `userId` is omitted."
)


TOOLS: list[Tool] = [
    Tool(
        name="mail_search",
        description="Search a mailbox for messages matching a query." + _MAILBOX,
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}, **USER_ID_PROPERTY},
            "required": ["query"],
        },
    ),
    Tool(
        name="mail_get",
        description="Get one message by id." + _MAILBOX,
        inputSchema={
            "type": "object",
            "properties": {"messageId": {"type": "string"}, **USER_ID_PROPERTY},
            "required": ["messageId"],
        },
    ),
    Tool(
        name="mail_list_attachments",
        description="List a message's attachments: id, name, type and size." + _MAILBOX,
        inputSchema={
            "type": "object",
            "properties": {"messageId": {"type": "string"}, **USER_ID_PROPERTY},
            "required": ["messageId"],
        },
    ),
    Tool(
        name="mail_get_attachment",
        description=(
            "Fetch one attachment of a message, with an id from mail_list_attachments. It is "
            "kept as a file of this run, whose id the result names: pass that id to attach "
            "it elsewhere." + _MAILBOX
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "messageId": {"type": "string"},
                "attachmentId": {"type": "string"},
                **USER_ID_PROPERTY,
            },
            "required": ["messageId", "attachmentId"],
        },
    ),
    Tool(
        name="mail_send",
        description="Send a new email." + _MAILBOX,
        inputSchema={
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "attachments": ATTACHMENTS_SCHEMA,
                **USER_ID_PROPERTY,
            },
            "required": ["to", "subject", "body"],
        },
    ),
    Tool(
        name="mail_reply",
        description="Reply to an existing message." + _MAILBOX,
        inputSchema={
            "type": "object",
            "properties": {
                "messageId": {"type": "string"},
                "body": {"type": "string"},
                "attachments": ATTACHMENTS_SCHEMA,
                **USER_ID_PROPERTY,
            },
            "required": ["messageId", "body"],
        },
    ),
    Tool(
        name="mail_create_draft",
        description="Create a draft email without sending it." + _MAILBOX,
        inputSchema={
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "attachments": ATTACHMENTS_SCHEMA,
                **USER_ID_PROPERTY,
            },
            "required": ["subject", "body"],
        },
    ),
    Tool(
        name="mail_move",
        description="Move a message to a different folder." + _MAILBOX,
        inputSchema={
            "type": "object",
            "properties": {
                "messageId": {"type": "string"},
                "destinationFolderId": {"type": "string"},
                **USER_ID_PROPERTY,
            },
            "required": ["messageId", "destinationFolderId"],
        },
    ),
    Tool(
        name="mail_delete",
        description="Delete a message." + _MAILBOX,
        inputSchema={
            "type": "object",
            "properties": {"messageId": {"type": "string"}, **USER_ID_PROPERTY},
            "required": ["messageId"],
        },
    ),
    Tool(
        name="mail_list_folders",
        description="List a mailbox's folders." + _MAILBOX,
        inputSchema={"type": "object", "properties": {**USER_ID_PROPERTY}},
    ),
    Tool(
        name="mail_mark_read",
        description="Mark a message read or unread." + _MAILBOX,
        inputSchema={
            "type": "object",
            "properties": {
                "messageId": {"type": "string"},
                "isRead": {"type": "boolean"},
                **USER_ID_PROPERTY,
            },
            "required": ["messageId"],
        },
    ),
]

CALL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
    "mail_search": mail_search,
    "mail_get": mail_get,
    "mail_send": mail_send,
    "mail_reply": mail_reply,
    "mail_create_draft": mail_create_draft,
    "mail_move": mail_move,
    "mail_delete": mail_delete,
    "mail_list_folders": mail_list_folders,
    "mail_mark_read": mail_mark_read,
    "mail_list_attachments": mail_list_attachments,
    "mail_get_attachment": mail_get_attachment,
}
