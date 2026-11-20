"""Site and course tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from mcp.types import Tool

from .. import client
from ..catalog import TOOL_FUNCTIONS
from ..shaping import MAX_LIMIT, page, pick_one, refusals, shape_course, to_timestamp

#: Moodle 4.1 LTS's version number; older sites are outside what this capa
#: is tested against.
MIN_VERSION = 2022112800

_SEARCH_KEYS = ("query", "id", "shortname", "idnumber", "category")
#: tool argument -> Moodle course field
_COURSE_FIELDS = {
    "fullname": "fullname",
    "shortname": "shortname",
    "category_id": "categoryid",
    "idnumber": "idnumber",
    "summary": "summary",
}
_PAGING = {
    "limit": {"type": "integer", "description": "Max items (default 50, max 200)."},
    "offset": {"type": "integer", "description": "Items to skip, for the next page."},
}
_COURSE_PROPERTIES: dict[str, Any] = {
    "fullname": {"type": "string"},
    "shortname": {"type": "string", "description": "Unique short name."},
    "category_id": {"type": "integer"},
    "idnumber": {"type": "string", "description": "Optional external id."},
    "summary": {"type": "string", "description": "Course description (HTML allowed)."},
    "start_date": {"type": "string", "description": "ISO date, e.g. 2026-09-01."},
    "end_date": {"type": "string", "description": "ISO date."},
    "visible": {"type": "boolean"},
}


async def moodle_site_info(args: Mapping[str, Any]) -> dict[str, Any]:
    info = await client.call("core_webservice_get_site_info")
    available = {f.get("name") for f in info.get("functions") or []}
    missing = [
        {"function": function, "tool": tool}
        for tool, functions in TOOL_FUNCTIONS.items()
        for function in functions
        if function not in available
    ]
    result: dict[str, Any] = {
        "site_name": info.get("sitename"),
        "site_url": info.get("siteurl"),
        "release": info.get("release"),
        "version": info.get("version"),
        "service_user": {
            "id": info.get("userid"),
            "fullname": info.get("fullname"),
            "username": info.get("username"),
        },
        "missing_functions": missing,
    }
    try:
        version = int(str(info.get("version") or "0").split(".")[0])
    except ValueError:
        version = 0
    if version < MIN_VERSION:
        result["version_warning"] = "This Moodle is older than 4.1 LTS; some tools may not work."
    return result


async def moodle_search_courses(args: Mapping[str, Any]) -> dict[str, Any]:
    field, value = pick_one(args, _SEARCH_KEYS)
    if field == "query":
        answer = await client.call(
            "core_course_search_courses",
            {
                "criterianame": "search",
                "criteriavalue": str(value).strip(),
                "page": 0,
                "perpage": MAX_LIMIT,
            },
        )
        found = answer.get("courses") or []
        total = max(int(answer.get("total") or 0), len(found))
        return page([shape_course(c) for c in found], args, total=total)
    answer = await client.call("core_course_get_courses_by_field", {"field": field, "value": value})
    return page([shape_course(c) for c in answer.get("courses") or []], args)


async def moodle_get_course_contents(args: Mapping[str, Any]) -> list[dict[str, Any]]:
    sections = await client.call("core_course_get_contents", {"courseid": int(args["course_id"])})
    return [
        {
            "section": section.get("section"),
            "name": section.get("name"),
            "visible": bool(section.get("visible", 1)),
            "modules": [
                {
                    "cmid": module.get("id"),
                    "instance": module.get("instance"),
                    "type": module.get("modname"),
                    "name": module.get("name"),
                    "visible": bool(module.get("visible", 1)),
                }
                for module in section.get("modules") or []
            ],
        }
        for section in sections or []
    ]


def _course_payload(args: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        moodle: args[key] for key, moodle in _COURSE_FIELDS.items() if args.get(key) is not None
    }
    if args.get("visible") is not None:
        payload["visible"] = 1 if args["visible"] else 0
    for key, moodle in (("start_date", "startdate"), ("end_date", "enddate")):
        if args.get(key):
            payload[moodle] = to_timestamp(args[key])
    return payload


async def moodle_create_course(args: Mapping[str, Any]) -> dict[str, Any]:
    created = await client.call("core_course_create_courses", {"courses": [_course_payload(args)]})
    return {
        "created": [{"id": c.get("id"), "shortname": c.get("shortname")} for c in created or []]
    }


async def moodle_update_course(args: Mapping[str, Any]) -> dict[str, Any]:
    payload = _course_payload(args)
    if not payload:
        raise ValueError("Nothing to change: give at least one field besides course_id.")
    course_id = int(args["course_id"])
    answer = await client.call(
        "core_course_update_courses", {"courses": [{"id": course_id, **payload}]}
    )
    refused = refusals(answer, course_id)
    if refused:
        raise ValueError(f"Moodle did not update course {course_id}: {refused}")
    return {"updated": course_id}


TOOLS: list[Tool] = [
    Tool(
        name="moodle_site_info",
        description=(
            "Check the Moodle connection: site name, Moodle version, the service user oc8 acts "
            "as, and which web service functions the oc8 external service is still missing "
            "(each with the tool it disables)."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="moodle_search_courses",
        description=(
            "Find courses. Give exactly one of: query (free-text search, first 200 matches), "
            "id, shortname, idnumber or category (a category id lists its courses)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "id": {"type": "integer"},
                "shortname": {"type": "string"},
                "idnumber": {"type": "string"},
                "category": {"type": "integer"},
                **_PAGING,
            },
        },
    ),
    Tool(
        name="moodle_get_course_contents",
        description=(
            "List a course's sections and activities. Each activity's `instance` is the id "
            "the assignment and forum tools take (assignment_id, forum_id); `cmid` is the "
            "course-module id."
        ),
        inputSchema={
            "type": "object",
            "properties": {"course_id": {"type": "integer"}},
            "required": ["course_id"],
        },
    ),
    Tool(
        name="moodle_create_course",
        description="Create a course in a category.",
        inputSchema={
            "type": "object",
            "properties": _COURSE_PROPERTIES,
            "required": ["fullname", "shortname", "category_id"],
        },
    ),
    Tool(
        name="moodle_update_course",
        description="Change a course's name, category, dates, summary or visibility.",
        inputSchema={
            "type": "object",
            "properties": {"course_id": {"type": "integer"}, **_COURSE_PROPERTIES},
            "required": ["course_id"],
        },
    ),
]

CALL_HANDLERS: dict[str, Callable[[Mapping[str, Any]], Awaitable[Any]]] = {
    "moodle_site_info": moodle_site_info,
    "moodle_search_courses": moodle_search_courses,
    "moodle_get_course_contents": moodle_get_course_contents,
    "moodle_create_course": moodle_create_course,
    "moodle_update_course": moodle_update_course,
}
