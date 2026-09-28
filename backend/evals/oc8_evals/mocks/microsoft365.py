"""Mock Microsoft 365 MCP server: the mail + calendar subset of
capas/microsoft365, same tool and argument names, deterministic file-backed
state. Launch: `python -m oc8_evals.mocks.microsoft365` with
OC8_EVAL_STATE_FILE set."""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types

from oc8_evals.mocks._server import Handler, serve
from oc8_evals.mocks._store import Store

DEFAULT_MAILBOX = "me@example.com"

TOOLS: list[types.Tool] = [
    types.Tool(
        name="mail_search",
        description="Search a mailbox for messages matching a query.",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    ),
    types.Tool(
        name="mail_get",
        description="Get one message by id.",
        inputSchema={
            "type": "object",
            "properties": {"messageId": {"type": "string"}},
            "required": ["messageId"],
        },
    ),
    types.Tool(
        name="mail_send",
        description="Send a new email.",
        inputSchema={
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    ),
    types.Tool(
        name="mail_reply",
        description="Reply to an existing message.",
        inputSchema={
            "type": "object",
            "properties": {"messageId": {"type": "string"}, "body": {"type": "string"}},
            "required": ["messageId", "body"],
        },
    ),
    types.Tool(
        name="mail_create_draft",
        description="Create a draft email without sending it.",
        inputSchema={
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "inReplyTo": {"type": "string", "description": "message id this drafts a reply to"},
            },
            "required": ["subject", "body"],
        },
    ),
    types.Tool(
        name="calendar_list_events",
        description="List events in a date range.",
        inputSchema={
            "type": "object",
            "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
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
        description="Create a calendar event.",
        inputSchema={
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "start": {"type": "string", "description": "ISO 8601 datetime"},
                "end": {"type": "string", "description": "ISO 8601 datetime"},
                "attendees": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["subject", "start", "end"],
        },
    ),
    types.Tool(
        name="calendar_check_free_busy",
        description="Check free/busy for a list of attendees in a time range.",
        inputSchema={
            "type": "object",
            "properties": {
                "attendees": {"type": "array", "items": {"type": "string"}},
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
            "required": ["attendees", "start", "end"],
        },
    ),
]


def _ts(value: str) -> str:
    return value.rstrip("Zz")


def _overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    a_start, a_end = _ts(a_start), _ts(a_end)
    b_start, b_end = _ts(b_start), _ts(b_end)
    return a_start < b_end and b_start < a_end


def handlers(store: Store) -> dict[str, Handler]:
    async def mail_search(args: dict[str, Any]) -> str:
        q = str(args["query"]).lower()
        hits = [
            msg
            for msg in store.read().get("messages", [])
            if q in msg["subject"].lower() or q in msg["body"].lower() or q in msg["from"].lower()
        ]
        return json.dumps(hits)

    async def mail_get(args: dict[str, Any]) -> str:
        for msg in store.read().get("messages", []):
            if msg["id"] == args["messageId"]:
                return json.dumps(msg)
        raise KeyError(f"no message {args['messageId']}")

    async def mail_send(args: dict[str, Any]) -> str:
        def _do(state: dict[str, Any]) -> str:
            mid = store.next_id(state, "msg")
            state.setdefault("messages", []).append(
                {
                    "id": mid,
                    "from": DEFAULT_MAILBOX,
                    "to": list(args["to"]),
                    "subject": str(args["subject"]),
                    "body": str(args["body"]),
                    "folder": "sent",
                    "inReplyTo": None,
                    "sent": True,
                }
            )
            return f"sent {mid}"

        return store.update(_do)

    async def mail_reply(args: dict[str, Any]) -> str:
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
                    "folder": "sent",
                    "inReplyTo": original["id"],
                    "sent": True,
                }
            )
            return f"sent {mid}"

        return store.update(_do)

    async def mail_create_draft(args: dict[str, Any]) -> str:
        def _do(state: dict[str, Any]) -> str:
            did = store.next_id(state, "draft")
            # A reply draft that names the message but omits `to` is still
            # addressed to that message's sender. mail_send already does this;
            # leaving it off the draft made "draft addressed to the customer"
            # fail whenever the model set inReplyTo and not to.
            recipients = list(args.get("to") or [])
            reply_to = args.get("inReplyTo")
            if not recipients and reply_to:
                original = next(
                    (m for m in state.get("messages", []) if m.get("id") == reply_to),
                    None,
                )
                if original and original.get("from"):
                    recipients = [original["from"]]
            state.setdefault("drafts", []).append(
                {
                    "id": did,
                    "to": recipients,
                    "subject": str(args["subject"]),
                    "body": str(args["body"]),
                    "inReplyTo": reply_to,
                }
            )
            return f"draft {did} created (not sent)"

        return store.update(_do)

    async def calendar_list_events(args: dict[str, Any]) -> str:
        start, end = str(args.get("start", "")), str(args.get("end", "9999"))
        events = [
            e for e in store.read().get("events", []) if _overlaps(e["start"], e["end"], start, end)
        ]
        return json.dumps(events)

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
                    "subject": str(args["subject"]),
                    "start": str(args["start"]),
                    "end": str(args["end"]),
                    "attendees": list(args.get("attendees", [])),
                }
            )
            return f"created event {eid}"

        return store.update(_do)

    async def calendar_check_free_busy(args: dict[str, Any]) -> str:
        busy = store.read().get("busy", {})
        start, end = str(args["start"]), str(args["end"])
        out = {
            a: [slot for slot in busy.get(a, []) if _overlaps(slot[0], slot[1], start, end)]
            for a in args["attendees"]
        }
        return json.dumps(out)

    return {
        "mail_search": mail_search,
        "mail_get": mail_get,
        "mail_send": mail_send,
        "mail_reply": mail_reply,
        "mail_create_draft": mail_create_draft,
        "calendar_list_events": calendar_list_events,
        "calendar_get_event": calendar_get_event,
        "calendar_create_event": calendar_create_event,
        "calendar_check_free_busy": calendar_check_free_busy,
    }


if __name__ == "__main__":
    serve("microsoft365-mock", TOOLS, handlers(Store.from_env()))
