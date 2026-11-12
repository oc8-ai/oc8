"""Participants, user accounts and manual enrolment."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from mcp.types import Tool

from .. import client
from ..shaping import has_role, page, pick_one, refusals, shape_user

#: Moodle's install-default role ids. Moodle 4.1 has no web service function
#: that lists roles, so names map to these; a site that renumbered its roles
#: passes `role_id` instead.
ROLE_IDS = {"student": 5, "teacher": 4, "editingteacher": 3}
_DAY = 86400
_USER_FIELDS = ("firstname", "lastname", "email", "idnumber")
#: What core_enrol_get_enrolled_users returns: only the fields shape_user and
#: the submission join read, instead of every profile field.
_ENROLLED_FIELDS = "id,fullname,email,username,roles,groups,lastaccess,lastcourseaccess,suspended"
_PAGING = {
    "limit": {"type": "integer", "description": "Max items (default 50, max 200)."},
    "offset": {"type": "integer", "description": "Items to skip, for the next page."},
}


async def enrolled_users(course_id: int) -> list[dict[str, Any]]:
    """People with an ACTIVE enrolment in a course: suspended or expired
    enrolments are left out, so they never count as students to chase."""
    users = await client.call(
        "core_enrol_get_enrolled_users",
        {
            "courseid": course_id,
            "options": [
                {"name": "onlyactive", "value": 1},
                {"name": "userfields", "value": _ENROLLED_FIELDS},
            ],
        },
    )
    return list(users or [])


async def moodle_list_participants(args: Mapping[str, Any]) -> dict[str, Any]:
    users = await enrolled_users(int(args["course_id"]))
    if args.get("role"):
        users = [u for u in users if has_role(u, str(args["role"]))]
    if args.get("only_inactive_days") is not None:
        cutoff = time.time() - int(args["only_inactive_days"]) * _DAY
        users = [
            u for u in users if not u.get("lastcourseaccess") or int(u["lastcourseaccess"]) < cutoff
        ]
    return page([shape_user(u) for u in users], args)


async def moodle_find_users(args: Mapping[str, Any]) -> list[dict[str, Any]]:
    field, value = pick_one(args, ("email", "username", "id", "idnumber"))
    users = await client.call("core_user_get_users_by_field", {"field": field, "values": [value]})
    return [shape_user(u) for u in users or []]


async def moodle_create_user(args: Mapping[str, Any]) -> dict[str, Any]:
    user: dict[str, Any] = {
        "username": str(args["username"]).strip().lower(),
        "firstname": args["firstname"],
        "lastname": args["lastname"],
        "email": args["email"],
        "auth": args.get("auth") or "manual",
    }
    if args.get("idnumber"):
        user["idnumber"] = args["idnumber"]
    if user["auth"] == "manual":
        # Moodle generates a password and emails it to the new user.
        user["createpassword"] = 1
    created = await client.call("core_user_create_users", {"users": [user]})
    return {"created": [{"id": u.get("id"), "username": u.get("username")} for u in created or []]}


async def moodle_update_user(args: Mapping[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {k: args[k] for k in _USER_FIELDS if args.get(k) is not None}
    if args.get("suspended") is not None:
        changes["suspended"] = 1 if args["suspended"] else 0
    if not changes:
        raise ValueError("Nothing to change: give at least one field besides user_id.")
    user_id = int(args["user_id"])
    answer = await client.call("core_user_update_users", {"users": [{"id": user_id, **changes}]})
    refused = refusals(answer, user_id)
    if refused:
        raise ValueError(f"Moodle did not update user {user_id}: {refused}")
    return {"updated": user_id}


async def moodle_enrol_user(args: Mapping[str, Any]) -> dict[str, Any]:
    if args.get("role_id") is not None:
        role_id = int(args["role_id"])
    else:
        role = str(args.get("role") or "student")
        if role not in ROLE_IDS:
            raise ValueError(
                f"Unknown role {role!r}: use student, teacher or editingteacher, or give role_id."
            )
        role_id = ROLE_IDS[role]
    course_id, user_id = int(args["course_id"]), int(args["user_id"])
    await client.call(
        "enrol_manual_enrol_users",
        {"enrolments": [{"roleid": role_id, "userid": user_id, "courseid": course_id}]},
    )
    return {"enrolled": True, "course_id": course_id, "user_id": user_id, "role_id": role_id}


async def moodle_unenrol_user(args: Mapping[str, Any]) -> dict[str, Any]:
    course_id, user_id = int(args["course_id"]), int(args["user_id"])
    await client.call(
        "enrol_manual_unenrol_users", {"enrolments": [{"userid": user_id, "courseid": course_id}]}
    )
    return {"unenrolled": True, "course_id": course_id, "user_id": user_id}


TOOLS: list[Tool] = [
    Tool(
        name="moodle_list_participants",
        description=(
            "List the people enrolled in a course with their roles, groups and last access. "
            "Filter by role (student, teacher, editingteacher) and with only_inactive_days to "
            "find people who have not opened the course for that many days (or never)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "course_id": {"type": "integer"},
                "role": {"type": "string"},
                "only_inactive_days": {"type": "integer"},
                **_PAGING,
            },
            "required": ["course_id"],
        },
    ),
    Tool(
        name="moodle_find_users",
        description="Look a user up by exactly one of: email, username, id or idnumber.",
        inputSchema={
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "username": {"type": "string"},
                "id": {"type": "integer"},
                "idnumber": {"type": "string"},
            },
        },
    ),
    Tool(
        name="moodle_create_user",
        description=(
            "Create a user account. With auth 'manual' (default) Moodle emails the new user "
            "a generated password; use the site's SSO method (e.g. 'oauth2', 'ldap') otherwise."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "username": {"type": "string"},
                "firstname": {"type": "string"},
                "lastname": {"type": "string"},
                "email": {"type": "string"},
                "auth": {"type": "string"},
                "idnumber": {"type": "string"},
            },
            "required": ["username", "firstname", "lastname", "email"],
        },
    ),
    Tool(
        name="moodle_update_user",
        description="Change a user's name, email or idnumber, or suspend / unsuspend them.",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "integer"},
                "firstname": {"type": "string"},
                "lastname": {"type": "string"},
                "email": {"type": "string"},
                "idnumber": {"type": "string"},
                "suspended": {"type": "boolean"},
            },
            "required": ["user_id"],
        },
    ),
    Tool(
        name="moodle_enrol_user",
        description=(
            "Enrol a user in a course (manual enrolment) as student (default), teacher or "
            "editingteacher. Give role_id instead if the site uses non-default role ids."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "course_id": {"type": "integer"},
                "user_id": {"type": "integer"},
                "role": {"type": "string", "enum": ["student", "teacher", "editingteacher"]},
                "role_id": {"type": "integer"},
            },
            "required": ["course_id", "user_id"],
        },
    ),
    Tool(
        name="moodle_unenrol_user",
        description="Remove a user's manual enrolment from a course.",
        inputSchema={
            "type": "object",
            "properties": {"course_id": {"type": "integer"}, "user_id": {"type": "integer"}},
            "required": ["course_id", "user_id"],
        },
    ),
]

CALL_HANDLERS: dict[str, Callable[[Mapping[str, Any]], Awaitable[Any]]] = {
    "moodle_list_participants": moodle_list_participants,
    "moodle_find_users": moodle_find_users,
    "moodle_create_user": moodle_create_user,
    "moodle_update_user": moodle_update_user,
    "moodle_enrol_user": moodle_enrol_user,
    "moodle_unenrol_user": moodle_unenrol_user,
}
