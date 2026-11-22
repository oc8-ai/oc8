"""The mail tools. Each handler reads the login from the bridge's environment
(core sets it from the agent's mailbox login when it launches the bridge,
through `credential_env_fields`), runs the blocking protocol work in a thread
and returns plain JSON-serialisable data; `__main__.py` wraps it for MCP.

A failure is raised as a `MailError`, whose message is safe to show: our own
wording, with the passwords removed from anything a server said.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp.types import Tool

from . import mailbox
from .mailbox import Account, MailError


@dataclass(frozen=True)
class FileContent:
    """A tool result that is a file. `__main__.py` sends it as MCP binary
    content, which core keeps as a file of the run; the model reads
    `details` and core's reference to the file, never the bytes."""

    name: str
    mime_type: str
    data: bytes
    details: dict[str, Any]


def _account() -> Account:
    return Account.from_env(os.environ)


async def _run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    account = _account()
    try:
        return await asyncio.to_thread(fn, account, *args, **kwargs)
    except MailError:
        raise
    except Exception as exc:
        text = mailbox.scrub(f"{type(exc).__name__}: {exc}", account)[:300]
        raise MailError(f"The mail tool failed: {text}") from None


def _text(args: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = args.get(key)
    if value is None or value == "":
        if required:
            raise MailError(f"`{key}` is required.")
        return ""
    if not isinstance(value, str | int):
        raise MailError(f"`{key}` must be text.")
    return str(value)


def _list(args: dict[str, Any], key: str, *, required: bool = False) -> list[str]:
    value = args.get(key)
    if value is None or value == [] or value == "":
        if required:
            raise MailError(f"`{key}` is required.")
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise MailError(f"`{key}` must be a list of addresses.")
    return list(value)


def _bool(args: dict[str, Any], key: str, default: bool) -> bool:
    value = args.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def _attachments(args: dict[str, Any]) -> list[dict[str, Any]]:
    value = args.get("attachments") or []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise MailError("`attachments` must be a list of {file} or {name, text | content_base64}.")
    return value


async def mail_list_folders(args: dict[str, Any]) -> Any:
    return await _run(mailbox.list_folders)


async def mail_search(args: dict[str, Any]) -> Any:
    limit = args.get("limit") or mailbox.DEFAULT_SEARCH_LIMIT
    if not isinstance(limit, int):
        raise MailError("`limit` must be a number.")
    return await _run(
        mailbox.search,
        folder=_text(args, "folder") or "INBOX",
        sender=_text(args, "from"),
        to=_text(args, "to"),
        subject=_text(args, "subject"),
        text=_text(args, "text"),
        since=_text(args, "since"),
        before=_text(args, "before"),
        unread_only=_bool(args, "unread_only", False),
        limit=limit,
    )


async def mail_get(args: dict[str, Any]) -> Any:
    return await _run(mailbox.get, _text(args, "id", required=True))


async def mail_get_attachment(args: dict[str, Any]) -> Any:
    found = await _run(
        mailbox.get_attachment,
        _text(args, "id", required=True),
        _text(args, "attachment_id", required=True),
    )
    data = found.pop("data")
    text = found.pop("text", None)
    truncated = found.pop("text_truncated", False)
    details = dict(found)
    if text is not None:
        details["content"] = text
        details["content_truncated"] = truncated
    return FileContent(name=found["name"], mime_type=found["type"], data=data, details=details)


async def mail_send(args: dict[str, Any]) -> Any:
    return await _run(
        mailbox.send,
        to=_list(args, "to", required=True),
        cc=_list(args, "cc"),
        bcc=_list(args, "bcc"),
        subject=_text(args, "subject"),
        body=_text(args, "body"),
        attachments=_attachments(args),
    )


async def mail_reply(args: dict[str, Any]) -> Any:
    return await _run(
        mailbox.reply,
        _text(args, "id", required=True),
        body=_text(args, "body", required=True),
        reply_all=_bool(args, "reply_all", False),
        attachments=_attachments(args),
    )


async def mail_forward(args: dict[str, Any]) -> Any:
    return await _run(
        mailbox.forward,
        _text(args, "id", required=True),
        to=_list(args, "to", required=True),
        cc=_list(args, "cc"),
        body=_text(args, "body"),
        attachments=_attachments(args),
    )


async def mail_move(args: dict[str, Any]) -> Any:
    return await _run(
        mailbox.move, _text(args, "id", required=True), _text(args, "folder", required=True)
    )


async def mail_mark_read(args: dict[str, Any]) -> Any:
    return await _run(
        mailbox.mark_read, _text(args, "id", required=True), _bool(args, "read", True)
    )


async def mail_delete(args: dict[str, Any]) -> Any:
    return await _run(mailbox.delete, _text(args, "id", required=True))


_ID = {
    "type": "string",
    "description": "The mail id from mail_search, mail_get or the new-mail event.",
}
_ADDRESSES = {"type": "array", "items": {"type": "string"}}
_ATTACHMENTS = {
    "type": "array",
    "description": (
        "Files to attach, at most 25 MB together. Each is {file} with the id of a "
        "file of this run (from mail_get_attachment or write_output_file), or "
        "{name, text} for a short text; `name` and `type` (a MIME type) override the file's."
    ),
    "items": {
        "type": "object",
        "properties": {
            # oc8 swaps the id for the file itself (oc8.agent.run_files).
            "file": {"type": "string", "description": "A run file's id.", "x-oc8-file": True},
            "name": {"type": "string"},
            "text": {"type": "string"},
            "content_base64": {
                "type": "string",
                "description": "Only where there is no file to name; prefer `file`.",
            },
            "type": {"type": "string"},
        },
    },
}
_POP3 = " On a POP3 mailbox this tool is not available."
_LIMITS = (
    " At most 50 recipients. The mail is marked Auto-Submitted: auto-replied. On IMAP a "
    "copy is kept in the Sent folder, unless the login turns that off or the provider "
    "keeps its own."
)

TOOLS: list[Tool] = [
    Tool(
        name="mail_list_folders",
        description="List the mailbox's folders." + _POP3,
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="mail_search",
        description=(
            "Search the mailbox, newest first. Every filter is optional; text filters "
            "match when the field contains the value. Returns each mail's id, sender, "
            "recipients, subject, date and whether it is unread. On POP3 only the inbox "
            "exists, there is no unread flag, and the newest 200 mails are searched."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Default INBOX."},
                "from": {"type": "string"},
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "text": {"type": "string", "description": "Anywhere in the mail."},
                "since": {"type": "string", "description": "YYYY-MM-DD, inclusive."},
                "before": {"type": "string", "description": "YYYY-MM-DD, exclusive."},
                "unread_only": {"type": "boolean"},
                "limit": {"type": "integer", "description": "1 to 100, default 20."},
            },
        },
    ),
    Tool(
        name="mail_get",
        description=(
            "Read one mail: its headers, its text (HTML converted to text, cut at "
            "20,000 characters) and its attachments with their ids. Does not mark it read."
        ),
        inputSchema={"type": "object", "properties": {"id": _ID}, "required": ["id"]},
    ),
    Tool(
        name="mail_get_attachment",
        description=(
            "Fetch one attachment of a mail (at most 25 MB). It is kept as a file of this "
            "run, whose id the result names: pass that id to attach it elsewhere. A text "
            "attachment's content is also shown, cut at 20,000 characters."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": _ID,
                "attachment_id": {"type": "string", "description": "From mail_get."},
            },
            "required": ["id", "attachment_id"],
        },
    ),
    Tool(
        name="mail_send",
        description="Send a new mail from this mailbox." + _LIMITS,
        inputSchema={
            "type": "object",
            "properties": {
                "to": _ADDRESSES,
                "cc": _ADDRESSES,
                "bcc": _ADDRESSES,
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain text."},
                "attachments": _ATTACHMENTS,
            },
            "required": ["to", "subject", "body"],
        },
    ),
    Tool(
        name="mail_reply",
        description=(
            "Reply to a mail, in its thread, quoting it. `reply_all` also answers "
            "everyone else it was sent to." + _LIMITS
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": _ID,
                "body": {"type": "string", "description": "Plain text."},
                "reply_all": {"type": "boolean"},
                "attachments": _ATTACHMENTS,
            },
            "required": ["id", "body"],
        },
    ),
    Tool(
        name="mail_forward",
        description=(
            "Forward a mail, attached whole, with an optional note above it and any "
            "further files." + _LIMITS
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": _ID,
                "to": _ADDRESSES,
                "cc": _ADDRESSES,
                "body": {"type": "string", "description": "Your note, plain text."},
                "attachments": _ATTACHMENTS,
            },
            "required": ["id", "to"],
        },
    ),
    Tool(
        name="mail_move",
        description="Move a mail to another folder." + _POP3,
        inputSchema={
            "type": "object",
            "properties": {"id": _ID, "folder": {"type": "string"}},
            "required": ["id", "folder"],
        },
    ),
    Tool(
        name="mail_mark_read",
        description="Mark a mail read, or unread with read=false." + _POP3,
        inputSchema={
            "type": "object",
            "properties": {"id": _ID, "read": {"type": "boolean"}},
            "required": ["id"],
        },
    ),
    Tool(
        name="mail_delete",
        description="Delete a mail for good.",
        inputSchema={"type": "object", "properties": {"id": _ID}, "required": ["id"]},
    ),
]

CALL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
    "mail_list_folders": mail_list_folders,
    "mail_search": mail_search,
    "mail_get": mail_get,
    "mail_get_attachment": mail_get_attachment,
    "mail_send": mail_send,
    "mail_reply": mail_reply,
    "mail_forward": mail_forward,
    "mail_move": mail_move,
    "mail_mark_read": mail_mark_read,
    "mail_delete": mail_delete,
}
