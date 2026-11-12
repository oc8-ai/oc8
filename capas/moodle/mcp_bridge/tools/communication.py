"""Forums and direct messages - the tools that reach learners."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from mcp.types import Tool

from .. import client
from ..shaping import MAX_LIMIT, as_html, iso, page, plain_text

MAX_RECIPIENTS = 50
#: Moodle's FORMAT_PLAIN.
_FORMAT_PLAIN = 2
_NO_RESULT = "Moodle returned no result for this recipient"
_PAGING = {
    "limit": {"type": "integer", "description": "Max items (default 50, max 200)."},
    "offset": {"type": "integer", "description": "Items to skip, for the next page."},
}


async def moodle_list_forum_discussions(args: Mapping[str, Any]) -> dict[str, Any]:
    if args.get("discussion_id") is not None:
        answer = await client.call(
            "mod_forum_get_discussion_posts", {"discussionid": int(args["discussion_id"])}
        )
        posts = [
            {
                "id": p.get("id"),
                "subject": p.get("subject"),
                "author": (p.get("author") or {}).get("fullname"),
                "created": iso(p.get("timecreated")),
                "parent_id": p.get("parentid"),
                "message": plain_text(p.get("message")),
            }
            for p in (answer or {}).get("posts") or []
        ]
        return page(posts, args)
    if args.get("forum_id") is None:
        raise ValueError(
            "Give forum_id to list a forum's discussions, or discussion_id to read its posts."
        )
    answer = await client.call(
        "mod_forum_get_forum_discussions",
        {"forumid": int(args["forum_id"]), "page": 0, "perpage": MAX_LIMIT},
    )
    discussions = [
        {
            "id": d.get("discussion"),
            "post_id": d.get("id"),
            "subject": d.get("name") or d.get("subject"),
            "author": d.get("userfullname"),
            "created": iso(d.get("created")),
            "replies": d.get("numreplies"),
            "last_post_at": iso(d.get("timemodified")),
            "message": plain_text(d.get("message"), 500),
        }
        for d in (answer or {}).get("discussions") or []
    ]
    result = page(discussions, args)
    if len(discussions) >= MAX_LIMIT:
        result["truncated"] = True
        result["note"] = (
            f"Only the {MAX_LIMIT} most recent discussions were fetched; older ones are not listed."
        )
    return result


async def moodle_send_message(args: Mapping[str, Any]) -> dict[str, Any]:
    user_ids = [int(u) for u in args.get("user_ids") or []]
    if not user_ids:
        raise ValueError("Give at least one recipient in user_ids.")
    if len(user_ids) > MAX_RECIPIENTS:
        raise ValueError(f"At most {MAX_RECIPIENTS} recipients per call.")
    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("The message text is empty.")
    answer = await client.call(
        "core_message_send_instant_messages",
        {
            "messages": [
                {"touserid": uid, "text": text, "textformat": _FORMAT_PLAIN} for uid in user_ids
            ]
        },
    )
    replies = answer if isinstance(answer, list) else []
    failed: list[dict[str, Any]] = []
    for index, uid in enumerate(user_ids):
        reply = replies[index] if index < len(replies) else None
        if not isinstance(reply, dict):
            failed.append({"user_id": uid, "error": _NO_RESULT})
        elif int(reply.get("msgid", -1)) == -1:
            failed.append({"user_id": uid, "error": reply.get("errormessage") or "not delivered"})
    return {"sent": len(user_ids) - len(failed), "failed": failed}


async def moodle_post_to_forum(args: Mapping[str, Any]) -> dict[str, Any]:
    has_forum, has_post = args.get("forum_id") is not None, args.get("post_id") is not None
    if has_forum and has_post:
        raise ValueError("Give forum_id for a new discussion or post_id for a reply, not both.")
    if not has_forum and not has_post:
        raise ValueError("Give forum_id for a new discussion or post_id for a reply.")
    message = str(args.get("message") or "").strip()
    if not message:
        raise ValueError("The post is empty.")
    if has_post:
        answer = await client.call(
            "mod_forum_add_discussion_post",
            {
                "postid": int(args["post_id"]),
                "subject": str(args.get("subject") or "Re:"),
                "message": as_html(message),
            },
        )
        return {"replied": True, "post_id": (answer or {}).get("postid")}
    subject = str(args.get("subject") or "").strip()
    if not subject:
        raise ValueError("A new discussion needs a subject.")
    answer = await client.call(
        "mod_forum_add_discussion",
        {"forumid": int(args["forum_id"]), "subject": subject, "message": as_html(message)},
    )
    return {"created": True, "discussion_id": (answer or {}).get("discussionid")}


TOOLS: list[Tool] = [
    Tool(
        name="moodle_list_forum_discussions",
        description=(
            "With forum_id: a forum's discussions (newest activity first). With "
            "discussion_id: that discussion's posts. forum_id is the forum activity's "
            "`instance` from moodle_get_course_contents."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "forum_id": {"type": "integer"},
                "discussion_id": {"type": "integer"},
                **_PAGING,
            },
        },
    ),
    Tool(
        name="moodle_send_message",
        description=(
            "Send a plain-text Moodle message to up to 50 users. Reports recipients Moodle "
            "could not deliver to (e.g. they blocked messages)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "user_ids": {"type": "array", "items": {"type": "integer"}, "maxItems": 50},
                "text": {"type": "string"},
            },
            "required": ["user_ids", "text"],
        },
    ),
    Tool(
        name="moodle_post_to_forum",
        description=(
            "Start a new discussion (forum_id + subject + message) or reply to a post "
            "(post_id + message). Forum subscribers are notified by Moodle."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "forum_id": {"type": "integer"},
                "post_id": {"type": "integer"},
                "subject": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["message"],
        },
    ),
]

CALL_HANDLERS: dict[str, Callable[[Mapping[str, Any]], Awaitable[Any]]] = {
    "moodle_list_forum_discussions": moodle_list_forum_discussions,
    "moodle_send_message": moodle_send_message,
    "moodle_post_to_forum": moodle_post_to_forum,
}
