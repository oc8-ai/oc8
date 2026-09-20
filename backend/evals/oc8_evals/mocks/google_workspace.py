"""Mock Google Workspace MCP server: the gmail + calendar + drive subset of
capas/google_workspace, same tool and argument names, file-backed state.
Launch: `python -m oc8_evals.mocks.google_workspace` with OC8_EVAL_STATE_FILE."""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types

from oc8_evals.mocks._server import Handler, serve
from oc8_evals.mocks._store import Store

DEFAULT_MAILBOX = "me@example.com"

_MAIL_FIELDS = {
    "to": {"type": "array", "items": {"type": "string"}},
    "cc": {"type": "array", "items": {"type": "string"}},
    "subject": {"type": "string"},
    "body": {"type": "string"},
}

TOOLS: list[types.Tool] = [
    types.Tool(
        name="gmail_search",
        description="Search messages matching a Gmail query.",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    ),
    types.Tool(
        name="gmail_get",
        description="Get one message by id.",
        inputSchema={
            "type": "object",
            "properties": {"messageId": {"type": "string"}},
            "required": ["messageId"],
        },
    ),
    types.Tool(
        name="gmail_send",
        description="Send a new email.",
        inputSchema={
            "type": "object",
            "properties": _MAIL_FIELDS,
            "required": ["to", "subject", "body"],
        },
    ),
    types.Tool(
        name="gmail_reply",
        description="Reply to an existing message.",
        inputSchema={
            "type": "object",
            "properties": {"messageId": {"type": "string"}, "body": {"type": "string"}},
            "required": ["messageId", "body"],
        },
    ),
    types.Tool(
        name="gmail_create_draft",
        description="Create a draft without sending it.",
        inputSchema={
            "type": "object",
            "properties": {**_MAIL_FIELDS, "inReplyTo": {"type": "string"}},
            "required": ["to", "subject", "body"],
        },
    ),
    types.Tool(
        name="calendar_list_events",
        description="List events in a window.",
        inputSchema={
            "type": "object",
            "properties": {"timeMin": {"type": "string"}, "timeMax": {"type": "string"}},
            "required": ["timeMin", "timeMax"],
        },
    ),
    types.Tool(
        name="calendar_get_event",
        description="Get one event by id.",
        inputSchema={
            "type": "object",
            "properties": {"eventId": {"type": "string"}},
            "required": ["eventId"],
        },
    ),
    types.Tool(
        name="calendar_create_event",
        description="Create an event.",
        inputSchema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "start", "end"],
        },
    ),
    types.Tool(
        name="calendar_check_free_busy",
        description="Check free/busy in a window for the acting mailbox and other calendars.",
        inputSchema={
            "type": "object",
            "properties": {
                "timeMin": {"type": "string"},
                "timeMax": {"type": "string"},
                "calendars": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["timeMin", "timeMax"],
        },
    ),
    types.Tool(
        name="drive_list",
        description="List files in a drive.",
        inputSchema={
            "type": "object",
            "properties": {"driveId": {"type": "string"}},
            "required": ["driveId"],
        },
    ),
    types.Tool(
        name="drive_get_content",
        description="Get a file's text content.",
        inputSchema={
            "type": "object",
            "properties": {"fileId": {"type": "string"}},
            "required": ["fileId"],
        },
    ),
    types.Tool(
        name="drive_upload",
        description="Upload a text file to a drive.",
        inputSchema={
            "type": "object",
            "properties": {
                "driveId": {"type": "string"},
                "name": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["driveId", "name", "content"],
        },
    ),
]


def _overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start < b_end and b_start < a_end


def handlers(store: Store) -> dict[str, Handler]:
    async def gmail_search(args: dict[str, Any]) -> str:
        q = str(args["query"]).lower()
        return json.dumps(
            [
                m
                for m in store.read().get("messages", [])
                if q in m["subject"].lower() or q in m["body"].lower() or q in m["from"].lower()
            ]
        )

    async def gmail_get(args: dict[str, Any]) -> str:
        for m in store.read().get("messages", []):
            if m["id"] == args["messageId"]:
                return json.dumps(m)
        raise KeyError(f"no message {args['messageId']}")

    async def gmail_send(args: dict[str, Any]) -> str:
        def _do(state: dict[str, Any]) -> str:
            mid = store.next_id(state, "msg")
            state.setdefault("messages", []).append(
                {
                    "id": mid,
                    "from": DEFAULT_MAILBOX,
                    "to": list(args["to"]),
                    "subject": str(args["subject"]),
                    "body": str(args["body"]),
                    "labels": ["SENT"],
                    "inReplyTo": None,
                    "sent": True,
                }
            )
            return f"sent {mid}"

        return store.update(_do)

    async def gmail_reply(args: dict[str, Any]) -> str:
        def _do(state: dict[str, Any]) -> str:
            original = next(
                (m for m in state.get("messages", []) if m["id"] == args["messageId"]), None
            )
            if original is None:
                raise KeyError(f"no message {args['messageId']}")
            mid = store.next_id(state, "msg")
            state["messages"].append(
                {
                    "id": mid,
                    "from": DEFAULT_MAILBOX,
                    "to": [original["from"]],
                    "subject": "Re: " + original["subject"],
                    "body": str(args["body"]),
                    "labels": ["SENT"],
                    "inReplyTo": original["id"],
                    "sent": True,
                }
            )
            return f"sent {mid}"

        return store.update(_do)

    async def gmail_create_draft(args: dict[str, Any]) -> str:
        def _do(state: dict[str, Any]) -> str:
            did = store.next_id(state, "draft")
            state.setdefault("drafts", []).append(
                {
                    "id": did,
                    "to": list(args.get("to", [])),
                    "subject": str(args["subject"]),
                    "body": str(args["body"]),
                    "inReplyTo": args.get("inReplyTo"),
                }
            )
            return f"draft {did} created (not sent)"

        return store.update(_do)

    async def calendar_list_events(args: dict[str, Any]) -> str:
        lo, hi = str(args["timeMin"]), str(args["timeMax"])
        return json.dumps(
            [e for e in store.read().get("events", []) if _overlaps(e["start"], e["end"], lo, hi)]
        )

    async def calendar_get_event(args: dict[str, Any]) -> str:
        for e in store.read().get("events", []):
            if e["id"] == args["eventId"]:
                return json.dumps(e)
        raise KeyError(f"no event {args['eventId']}")

    async def calendar_create_event(args: dict[str, Any]) -> str:
        def _do(state: dict[str, Any]) -> str:
            eid = store.next_id(state, "evt")
            state.setdefault("events", []).append(
                {
                    "id": eid,
                    "summary": str(args["summary"]),
                    "start": str(args["start"]),
                    "end": str(args["end"]),
                    "attendees": list(args.get("attendees", [])),
                }
            )
            return f"created event {eid}"

        return store.update(_do)

    async def calendar_check_free_busy(args: dict[str, Any]) -> str:
        busy = store.read().get("busy", {})
        lo, hi = str(args["timeMin"]), str(args["timeMax"])
        calendars = [DEFAULT_MAILBOX, *args.get("calendars", [])]
        return json.dumps(
            {
                c: [slot for slot in busy.get(c, []) if _overlaps(slot[0], slot[1], lo, hi)]
                for c in calendars
            }
        )

    async def drive_list(args: dict[str, Any]) -> str:
        return json.dumps(
            [
                {"id": f["id"], "name": f["name"]}
                for f in store.read().get("files", [])
                if f["driveId"] == args["driveId"]
            ]
        )

    async def drive_get_content(args: dict[str, Any]) -> str:
        for f in store.read().get("files", []):
            if f["id"] == args["fileId"]:
                return str(f["content"])
        raise KeyError(f"no file {args['fileId']}")

    async def drive_upload(args: dict[str, Any]) -> str:
        def _do(state: dict[str, Any]) -> str:
            fid = store.next_id(state, "file")
            state.setdefault("files", []).append(
                {
                    "id": fid,
                    "driveId": str(args["driveId"]),
                    "name": str(args["name"]),
                    "content": str(args["content"]),
                }
            )
            return f"uploaded {fid}"

        return store.update(_do)

    return {
        "gmail_search": gmail_search,
        "gmail_get": gmail_get,
        "gmail_send": gmail_send,
        "gmail_reply": gmail_reply,
        "gmail_create_draft": gmail_create_draft,
        "calendar_list_events": calendar_list_events,
        "calendar_get_event": calendar_get_event,
        "calendar_create_event": calendar_create_event,
        "calendar_check_free_busy": calendar_check_free_busy,
        "drive_list": drive_list,
        "drive_get_content": drive_get_content,
        "drive_upload": drive_upload,
    }


if __name__ == "__main__":
    serve("google-workspace-mock", TOOLS, handlers(Store.from_env()))
