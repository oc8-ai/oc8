"""Outlook Calendar tools (design doc §4.2).

Every path is built by `_user.user_base`: `/users/{id}/...` for an app-only
token, `/me/...` for a person's own -- see `_user.py`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import Tool

from .. import graph
from .._user import USER_ID_PROPERTY, user_base

_TIMEZONE = "UTC"
_RANGE_PAGE_SIZE = 50


async def calendar_list_events(args: dict[str, Any]) -> Any:
    base = user_base(args)
    start = str(args.get("start") or "").strip()
    end = str(args.get("end") or "").strip()
    if start and end:
        # Only `/calendarView` honours a date range (and expands recurring
        # events into their occurrences); `/events` ignores startDateTime and
        # endDateTime and returns its first page regardless.
        result = await graph.get(
            f"{base}/calendarView",
            params={
                "startDateTime": start,
                "endDateTime": end,
                "$orderby": "start/dateTime",
                "$top": _RANGE_PAGE_SIZE,
            },
        )
    else:
        result = await graph.get(f"{base}/events", params={"$orderby": "start/dateTime"})
    return result.get("value", [])


async def calendar_get_event(args: dict[str, Any]) -> Any:
    base = user_base(args)
    return await graph.get(f"{base}/events/{args['eventId']}")


def _time_block(iso: str) -> dict[str, str]:
    return {"dateTime": iso, "timeZone": _TIMEZONE}


async def calendar_create_event(args: dict[str, Any]) -> Any:
    base = user_base(args)
    return await graph.post(
        f"{base}/events",
        {
            "subject": args["subject"],
            "start": _time_block(args["start"]),
            "end": _time_block(args["end"]),
            "attendees": [
                {"emailAddress": {"address": addr}} for addr in args.get("attendees", [])
            ],
        },
    )


async def calendar_update_event(args: dict[str, Any]) -> Any:
    base = user_base(args)
    body: dict[str, Any] = {}
    if "subject" in args:
        body["subject"] = args["subject"]
    if "start" in args:
        body["start"] = _time_block(args["start"])
    if "end" in args:
        body["end"] = _time_block(args["end"])
    return await graph.patch(f"{base}/events/{args['eventId']}", body)


async def calendar_cancel_event(args: dict[str, Any]) -> Any:
    base = user_base(args)
    await graph.post(
        f"{base}/events/{args['eventId']}/cancel", {"comment": args.get("comment", "")}
    )
    return {"status": "cancelled"}


async def calendar_respond_to_invite(args: dict[str, Any]) -> Any:
    base = user_base(args)
    verb = {"accept": "accept", "decline": "decline", "tentative": "tentativelyAccept"}[
        args["response"]
    ]
    await graph.post(f"{base}/events/{args['eventId']}/{verb}", {})
    return {"status": args["response"]}


async def calendar_check_free_busy(args: dict[str, Any]) -> Any:
    base = user_base(args)
    return await graph.post(
        f"{base}/calendar/getSchedule",
        {
            "schedules": args["attendees"],
            "startTime": _time_block(args["start"]),
            "endTime": _time_block(args["end"]),
        },
    )


#: Appended to every description below -- same reasoning as mail.py's own.
_CALENDAR = (
    " Acts on the calendar named by `userId`, or the connection's configured "
    "default mailbox when `userId` is omitted."
)


TOOLS: list[Tool] = [
    Tool(
        name="calendar_list_events",
        description=(
            "List events. Pass both `start` and `end` (ISO 8601) to get the events "
            "in that range; without them the calendar's first events are returned."
        )
        + _CALENDAR,
        inputSchema={
            "type": "object",
            "properties": {
                "start": {"type": "string"},
                "end": {"type": "string"},
                **USER_ID_PROPERTY,
            },
        },
    ),
    Tool(
        name="calendar_get_event",
        description="Get one event by id." + _CALENDAR,
        inputSchema={
            "type": "object",
            "properties": {"eventId": {"type": "string"}, **USER_ID_PROPERTY},
            "required": ["eventId"],
        },
    ),
    Tool(
        name="calendar_create_event",
        description="Create a calendar event." + _CALENDAR,
        inputSchema={
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "start": {"type": "string", "description": "ISO 8601 datetime"},
                "end": {"type": "string", "description": "ISO 8601 datetime"},
                "attendees": {"type": "array", "items": {"type": "string"}},
                **USER_ID_PROPERTY,
            },
            "required": ["subject", "start", "end"],
        },
    ),
    Tool(
        name="calendar_update_event",
        description="Update an existing event's subject/start/end." + _CALENDAR,
        inputSchema={
            "type": "object",
            "properties": {
                "eventId": {"type": "string"},
                "subject": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                **USER_ID_PROPERTY,
            },
            "required": ["eventId"],
        },
    ),
    Tool(
        name="calendar_cancel_event",
        description="Cancel an event." + _CALENDAR,
        inputSchema={
            "type": "object",
            "properties": {
                "eventId": {"type": "string"},
                "comment": {"type": "string"},
                **USER_ID_PROPERTY,
            },
            "required": ["eventId"],
        },
    ),
    Tool(
        name="calendar_respond_to_invite",
        description="Accept, decline, or tentatively accept an invite." + _CALENDAR,
        inputSchema={
            "type": "object",
            "properties": {
                "eventId": {"type": "string"},
                "response": {"type": "string", "enum": ["accept", "decline", "tentative"]},
                **USER_ID_PROPERTY,
            },
            "required": ["eventId", "response"],
        },
    ),
    Tool(
        name="calendar_check_free_busy",
        description="Check free/busy for a list of attendees in a time range." + _CALENDAR,
        inputSchema={
            "type": "object",
            "properties": {
                "attendees": {"type": "array", "items": {"type": "string"}},
                "start": {"type": "string"},
                "end": {"type": "string"},
                **USER_ID_PROPERTY,
            },
            "required": ["attendees", "start", "end"],
        },
    ),
]

CALL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
    "calendar_list_events": calendar_list_events,
    "calendar_get_event": calendar_get_event,
    "calendar_create_event": calendar_create_event,
    "calendar_update_event": calendar_update_event,
    "calendar_cancel_event": calendar_cancel_event,
    "calendar_respond_to_invite": calendar_respond_to_invite,
    "calendar_check_free_busy": calendar_check_free_busy,
}
