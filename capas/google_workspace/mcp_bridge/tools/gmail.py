"""Gmail tools. Every tool takes an optional `mailbox`; omitted, it falls back
to this connection's configured default (see google_api.resolve_mailbox_token).
Gmail's label model folds move/archive/mark-read into one operation --
`gmail_modify_labels` covers all three, matching the design doc's own note."""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from email.message import EmailMessage
from typing import Any

from mcp.types import Tool

from .. import google_api


def _mailbox_schema_props() -> dict[str, Any]:
    return {
        "mailbox": {
            "type": "string",
            "description": "Mailbox address; omit to use this connection's default.",
        }
    }


async def gmail_search(args: dict[str, Any]) -> Any:
    token = google_api.resolve_mailbox_token(args.get("mailbox"))
    mailbox = args.get("mailbox") or "me"
    return await google_api.get_json(
        google_api.API_GMAIL,
        f"/users/{mailbox}/messages",
        token=token,
        params={"q": args.get("query", "")},
    )


def _headers_map(payload: dict[str, Any]) -> dict[str, str]:
    """`{header-name-lowercased: value}`.

    RFC 5322 header names are case-insensitive, and the Gmail API returns each
    header's name AS IT APPEARED in the original message -- `Message-Id` is at
    least as common in the wild as `Message-ID`. An exact-case lookup therefore
    misses on real mail from real senders, and misses SILENTLY (a `.get` that
    returns `""` and a reply that renders unthreaded), so every read of these
    headers goes through one casefolded map."""
    return {
        str(h.get("name", "")).lower(): str(h.get("value", "")) for h in payload.get("headers", [])
    }


def _decode_body_data(data: str) -> str:
    """Gmail's `body.data` is base64url with the padding stripped."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="replace")


def _extract_body(payload: dict[str, Any]) -> tuple[str, str]:
    """`(mime_type, text)` for the best body part in `payload`.

    `users.messages.get` with the default `format=full` returns the body
    base64url-ENCODED, under `payload.body.data` for a simple message and under
    `payload.parts[].body.data` (recursively -- `multipart/alternative` nested
    inside `multipart/mixed` is the normal shape once an attachment is present)
    for a multipart one. Handing that JSON back verbatim gives the model an
    unreadable blob and only the ~200-char `snippet` in plain text.

    `text/plain` wins; `text/html` is the fallback and is REPORTED as such
    rather than passed off as prose, because raw markup silently presented as
    plain text is its own wrong answer."""
    plain = _find_part(payload, "text/plain")
    if plain is not None:
        return "text/plain", plain
    html = _find_part(payload, "text/html")
    if html is not None:
        return "text/html", html
    return "", ""


def _find_part(payload: dict[str, Any], mime_type: str) -> str | None:
    if payload.get("mimeType") == mime_type:
        data = payload.get("body", {}).get("data")
        if data:
            return _decode_body_data(str(data))
    for part in payload.get("parts", []) or []:
        found = _find_part(part, mime_type)
        if found is not None:
            return found
    return None


async def gmail_get(args: dict[str, Any]) -> Any:
    """Returns the decoded message, not the raw API JSON -- see
    `_extract_body`."""
    token = google_api.resolve_mailbox_token(args.get("mailbox"))
    mailbox = args.get("mailbox") or "me"
    message = await google_api.get_json(
        google_api.API_GMAIL,
        f"/users/{mailbox}/messages/{args['messageId']}",
        token=token,
    )
    payload = message.get("payload", {})
    body_mime_type, body = _extract_body(payload)
    return {
        "id": message.get("id", ""),
        "threadId": message.get("threadId", ""),
        "labelIds": message.get("labelIds", []),
        "headers": _headers_map(payload),
        "snippet": message.get("snippet", ""),
        "body": body,
        "bodyMimeType": body_mime_type,
    }


def _build_raw_message(
    args: dict[str, Any], *, extra_headers: dict[str, str] | None = None
) -> str:
    """An LLM cannot reliably produce base64url-encoded RFC 2822 bytes -- the
    sibling Microsoft plugin's `mail_send` takes structured to/subject/body
    for exactly this reason. Build the MIME message here, from structured
    inputs, and only base64url-encode it at the boundary."""
    msg = EmailMessage()
    msg["To"] = ", ".join(args["to"])
    if args.get("cc"):
        msg["Cc"] = ", ".join(args["cc"])
    msg["Subject"] = args.get("subject", "")
    msg.set_content(args.get("body", ""))
    for header, value in (extra_headers or {}).items():
        msg[header] = value
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


async def gmail_send(args: dict[str, Any]) -> Any:
    token = google_api.resolve_mailbox_token(args.get("mailbox"))
    mailbox = args.get("mailbox") or "me"
    raw = _build_raw_message(args)
    return await google_api.post_json(
        google_api.API_GMAIL,
        f"/users/{mailbox}/messages/send",
        token=token,
        json={"raw": raw},
    )


async def gmail_reply(args: dict[str, Any]) -> Any:
    """Threading a reply needs more than `threadId` -- Gmail requires the
    reply's own MIME to carry `In-Reply-To`/`References` set to the original
    message's `Message-ID`, or the reply lands in the thread server-side but
    renders unthreaded in most mail clients. Fetch the original's headers
    first (a cheap metadata-only read), then build the reply's MIME with them.

    The message replied to is `messageId`; `threadId` is still taken in its
    place for older callers, but is only ever the message's id. The reply goes
    into the thread Gmail reports for that message -- a message id is not its
    thread id once the thread has more than one message."""
    message = str(args.get("messageId") or args.get("threadId") or "")
    if not message:
        raise RuntimeError("gmail_reply needs messageId, the id of the message replied to")
    token = google_api.resolve_mailbox_token(args.get("mailbox"))
    mailbox = args.get("mailbox") or "me"
    original = await google_api.get_json(
        google_api.API_GMAIL,
        f"/users/{mailbox}/messages/{message}",
        token=token,
        params={"format": "metadata", "metadataHeaders": ["Message-ID", "Subject"]},
    )
    original_headers = _headers_map(original.get("payload", {}))
    message_id = original_headers.get("message-id", "")
    original_subject = original_headers.get("subject", "")
    reply_args = dict(args)
    reply_args["subject"] = args.get("subject") or (
        original_subject
        if original_subject.lower().startswith("re:")
        else f"Re: {original_subject}"
    )
    extra_headers = (
        {"In-Reply-To": message_id, "References": message_id} if message_id else {}
    )
    raw = _build_raw_message(reply_args, extra_headers=extra_headers)
    return await google_api.post_json(
        google_api.API_GMAIL,
        f"/users/{mailbox}/messages/send",
        token=token,
        json={"raw": raw, "threadId": str(original.get("threadId") or message)},
    )


async def gmail_create_draft(args: dict[str, Any]) -> Any:
    token = google_api.resolve_mailbox_token(args.get("mailbox"))
    mailbox = args.get("mailbox") or "me"
    raw = _build_raw_message(args)
    return await google_api.post_json(
        google_api.API_GMAIL,
        f"/users/{mailbox}/drafts",
        token=token,
        json={"message": {"raw": raw}},
    )


async def gmail_list_labels(args: dict[str, Any]) -> Any:
    token = google_api.resolve_mailbox_token(args.get("mailbox"))
    mailbox = args.get("mailbox") or "me"
    return await google_api.get_json(
        google_api.API_GMAIL, f"/users/{mailbox}/labels", token=token
    )


async def gmail_modify_labels(args: dict[str, Any]) -> Any:
    token = google_api.resolve_mailbox_token(args.get("mailbox"))
    mailbox = args.get("mailbox") or "me"
    return await google_api.post_json(
        google_api.API_GMAIL,
        f"/users/{mailbox}/messages/{args['messageId']}/modify",
        token=token,
        json={
            "addLabelIds": args.get("addLabelIds", []),
            "removeLabelIds": args.get("removeLabelIds", []),
        },
    )


TOOLS: list[Tool] = [
    Tool(
        name="gmail_search",
        description="Search a mailbox's messages (Gmail search syntax).",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}, **_mailbox_schema_props()},
            "required": ["query"],
        },
    ),
    Tool(
        name="gmail_get",
        description=(
            "Get one message by id. Returns the message's headers, snippet and its body "
            "DECODED to readable text (Gmail returns it base64url-encoded). `bodyMimeType` "
            "says whether the body is text/plain or, when the message has no plain-text "
            "part, text/html markup."
        ),
        inputSchema={
            "type": "object",
            "properties": {"messageId": {"type": "string"}, **_mailbox_schema_props()},
            "required": ["messageId"],
        },
    ),
    Tool(
        name="gmail_send",
        description="Send a message. The tool builds the MIME message from these structured fields -- do not pass a pre-encoded raw message.",
        inputSchema={
            "type": "object",
            "properties": {
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recipient email addresses.",
                },
                "cc": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                **_mailbox_schema_props(),
            },
            "required": ["to", "subject", "body"],
        },
    ),
    Tool(
        name="gmail_reply",
        description="Reply within an existing thread. The tool fetches the original message's headers to thread the reply correctly (In-Reply-To/References) and builds the MIME message from these structured fields.",
        inputSchema={
            "type": "object",
            "properties": {
                "messageId": {
                    "type": "string",
                    "description": "The id of the message replied to (as gmail_get takes it).",
                },
                "threadId": {
                    "type": "string",
                    "description": "Deprecated: the id of the message replied to, as messageId.",
                },
                "to": {"type": "array", "items": {"type": "string"}},
                "cc": {"type": "array", "items": {"type": "string"}},
                "subject": {
                    "type": "string",
                    "description": "Optional; defaults to the original subject, Re:-prefixed.",
                },
                "body": {"type": "string"},
                **_mailbox_schema_props(),
            },
            "required": ["to", "body"],
        },
    ),
    Tool(
        name="gmail_create_draft",
        description="Create a draft message (never sent), built from these structured fields.",
        inputSchema={
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}},
                "cc": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                **_mailbox_schema_props(),
            },
            "required": ["to", "subject", "body"],
        },
    ),
    Tool(
        name="gmail_list_labels",
        description="List a mailbox's labels.",
        inputSchema={"type": "object", "properties": {**_mailbox_schema_props()}},
    ),
    Tool(
        name="gmail_modify_labels",
        description="Add/remove labels on a message — this is how move, archive, and mark-read/unread work in Gmail's model.",
        inputSchema={
            "type": "object",
            "properties": {
                "messageId": {"type": "string"},
                "addLabelIds": {"type": "array", "items": {"type": "string"}},
                "removeLabelIds": {"type": "array", "items": {"type": "string"}},
                **_mailbox_schema_props(),
            },
            "required": ["messageId"],
        },
    ),
]

CALL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
    "gmail_search": gmail_search,
    "gmail_get": gmail_get,
    "gmail_send": gmail_send,
    "gmail_reply": gmail_reply,
    "gmail_create_draft": gmail_create_draft,
    "gmail_list_labels": gmail_list_labels,
    "gmail_modify_labels": gmail_modify_labels,
}
