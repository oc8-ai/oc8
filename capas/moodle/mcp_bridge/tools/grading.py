"""Grades, completion, assignments and submissions."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from mcp.types import Tool

from .. import client
from ..shaping import as_html, has_role, iso, page, plain_text
from .people import enrolled_users

_COMPLETION_STATES = {0: "incomplete", 1: "complete", 2: "complete_pass", 3: "complete_fail"}
#: Moodle's FORMAT_HTML: feedback is stored as HTML.
_FORMAT_HTML = 1
_PAGING = {
    "limit": {"type": "integer", "description": "Max items (default 50, max 200)."},
    "offset": {"type": "integer", "description": "Items to skip, for the next page."},
}


def _grade_item(item: Mapping[str, Any]) -> dict[str, Any]:
    name = item.get("itemname") or ("Course total" if item.get("itemtype") == "course" else "")
    grade = item.get("gradeformatted")
    percentage = item.get("percentageformatted")
    return {
        "name": name,
        "type": item.get("itemmodule") or item.get("itemtype"),
        "grade": grade if grade not in (None, "", "-") else None,
        "raw": item.get("graderaw"),
        "max": item.get("grademax"),
        "percentage": percentage if percentage not in (None, "", "-") else None,
        "feedback": plain_text(item.get("feedback"), 1000),
    }


async def moodle_get_grades(args: Mapping[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {"courseid": int(args["course_id"])}
    if args.get("user_id") is not None:
        params["userid"] = int(args["user_id"])
    answer = await client.call("gradereport_user_get_grade_items", params)
    rows = [
        {
            "user_id": user.get("userid"),
            "fullname": user.get("userfullname"),
            "items": [_grade_item(i) for i in user.get("gradeitems") or []],
        }
        for user in (answer or {}).get("usergrades") or []
    ]
    return page(rows, args)


async def moodle_get_completion(args: Mapping[str, Any]) -> list[dict[str, Any]]:
    answer = await client.call(
        "core_completion_get_activities_completion_status",
        {"courseid": int(args["course_id"]), "userid": int(args["user_id"])},
    )
    return [
        {
            "cmid": status.get("cmid"),
            "type": status.get("modname"),
            "instance": status.get("instance"),
            "state": _COMPLETION_STATES.get(int(status.get("state") or 0), "unknown"),
            "completed_at": iso(status.get("timecompleted")),
        }
        for status in (answer or {}).get("statuses") or []
    ]


async def moodle_list_assignments(args: Mapping[str, Any]) -> list[dict[str, Any]]:
    course_id = int(args["course_id"])
    # includenotenrolledcourses: the service user is usually a manager, not enrolled.
    answer = await client.call(
        "mod_assign_get_assignments",
        {"courseids": [course_id], "includenotenrolledcourses": 1},
    )
    course = next(
        (c for c in (answer or {}).get("courses") or [] if c.get("id") == course_id), None
    )
    if course is None:
        reasons = "; ".join(
            str(w.get("message")) for w in (answer or {}).get("warnings") or [] if w.get("message")
        )
        raise ValueError(
            f"Moodle returned no assignments for course {course_id}"
            + (f": {reasons}" if reasons else "")
        )
    assignments = course.get("assignments") or []
    return [
        {
            "id": a.get("id"),
            "cmid": a.get("cmid"),
            "name": a.get("name"),
            "due": iso(a.get("duedate")),
            "cutoff": iso(a.get("cutoffdate")),
            "opens": iso(a.get("allowsubmissionsfromdate")),
            "max_grade": a.get("grade"),
            "intro": plain_text(a.get("intro"), 500),
        }
        for a in assignments
    ]


async def _submission_with_text(assignment_id: int, user_id: int) -> dict[str, Any]:
    answer = await client.call(
        "mod_assign_get_submission_status", {"assignid": assignment_id, "userid": user_id}
    )
    submission = ((answer or {}).get("lastattempt") or {}).get("submission") or {}
    plugins = submission.get("plugins") or []
    text = "".join(
        field.get("text") or ""
        for plugin in plugins
        if plugin.get("type") == "onlinetext"
        for field in plugin.get("editorfields") or []
    )
    files = [
        f["filename"]
        for plugin in plugins
        for area in plugin.get("fileareas") or []
        for f in area.get("files") or []
        if f.get("filename")
    ]
    feedback = (answer or {}).get("feedback") or {}
    grade = plain_text(feedback.get("gradefordisplay"), 200) or None
    return {
        "user_id": user_id,
        "status": submission.get("status") or "new",
        "submitted_at": iso(submission.get("timemodified")),
        "text": plain_text(text),
        "files": files,
        "grade": grade,
    }


async def moodle_list_submissions(args: Mapping[str, Any]) -> Any:
    assignment_id = int(args["assignment_id"])
    if args.get("include_text"):
        if args.get("user_id") is None:
            raise ValueError(
                "include_text needs user_id: submission text is fetched one student at a time."
            )
        return await _submission_with_text(assignment_id, int(args["user_id"]))
    if args.get("course_id") is None:
        raise ValueError("course_id is required to find students who have not submitted.")
    answer = await client.call("mod_assign_get_submissions", {"assignmentids": [assignment_id]})
    assignments = (answer or {}).get("assignments") or []
    match = next((a for a in assignments if a.get("assignmentid") == assignment_id), None)
    if match is None:
        reasons = "; ".join(
            str(w.get("message")) for w in (answer or {}).get("warnings") or [] if w.get("message")
        )
        raise ValueError(
            f"Moodle returned no submissions for assignment {assignment_id}"
            + (f": {reasons}" if reasons else " (wrong id or no access?).")
        )
    submissions = match.get("submissions") or []
    rows_by_user = {s.get("userid"): s for s in submissions if s.get("userid")}
    # Team assignments: one row per group (userid 0), shared by its members.
    rows_by_group = {
        s.get("groupid"): s for s in submissions if not s.get("userid") and s.get("groupid")
    }
    enrolled = await enrolled_users(int(args["course_id"]))
    rows: list[dict[str, Any]] = []
    for student in [u for u in enrolled if has_role(u, "student")]:
        submission = rows_by_user.get(student.get("id")) or next(
            (
                rows_by_group[g.get("id")]
                for g in student.get("groups") or []
                if g.get("id") in rows_by_group
            ),
            {},
        )
        status = submission.get("status") or "new"
        rows.append(
            {
                "user_id": student.get("id"),
                "fullname": student.get("fullname"),
                "status": status,
                "submitted_at": iso(submission.get("timemodified"))
                if status == "submitted"
                else None,
                "grading_status": submission.get("gradingstatus") or "notgraded",
            }
        )
    if args.get("user_id") is not None:
        rows = [r for r in rows if r["user_id"] == int(args["user_id"])]
    if args.get("status"):
        rows = [r for r in rows if r["status"] == args["status"]]
    return page(rows, args)


async def _max_grade(course_id: int, assignment_id: int) -> float:
    """The assignment's maximum grade; negative means it is graded on a scale."""
    answer = await client.call(
        "mod_assign_get_assignments",
        {"courseids": [course_id], "includenotenrolledcourses": 1},
    )
    assignment = next(
        (
            a
            for c in (answer or {}).get("courses") or []
            if c.get("id") == course_id
            for a in c.get("assignments") or []
            if a.get("id") == assignment_id
        ),
        None,
    )
    if assignment is None:
        raise ValueError(f"Assignment {assignment_id} was not found in course {course_id}.")
    return float(assignment.get("grade") or 0)


async def moodle_grade_submission(args: Mapping[str, Any]) -> dict[str, Any]:
    assignment_id, user_id = int(args["assignment_id"]), int(args["user_id"])
    if args.get("course_id") is None:
        raise ValueError("course_id is required to check the assignment's maximum grade.")
    course_id = int(args["course_id"])
    grade = float(args["grade"])
    if not math.isfinite(grade):
        raise ValueError(f"{args['grade']!r} is not a valid grade.")
    if grade < 0:
        raise ValueError(f"Grade {grade:g} cannot be negative.")
    maximum = await _max_grade(course_id, assignment_id)
    if maximum < 0:
        raise ValueError("This assignment uses a scale; grade it in Moodle.")
    if grade > maximum:
        raise ValueError(f"Grade {grade:g} is above the maximum {maximum:g} for this assignment.")
    params: dict[str, Any] = {
        "assignmentid": assignment_id,
        "userid": user_id,
        "grade": grade,
        "attemptnumber": -1,
        "addattempt": 0,
        "workflowstate": "",
        "applytoall": 1,
    }
    if args.get("feedback"):
        feedback = str(args["feedback"])
        if "<" not in feedback:
            # Plain text: keep the model's line breaks once Moodle renders HTML.
            feedback = as_html(feedback)
        params["plugindata"] = {
            "assignfeedbackcomments_editor": {"text": feedback, "format": _FORMAT_HTML}
        }
    await client.call("mod_assign_save_grade", params)
    return {"graded": True, "assignment_id": assignment_id, "user_id": user_id, "grade": grade}


TOOLS: list[Tool] = [
    Tool(
        name="moodle_get_grades",
        description=(
            "Grade items of a course (assignments, quizzes, course total) with grade, "
            "percentage and feedback - for one user, or for every participant when user_id "
            "is omitted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "course_id": {"type": "integer"},
                "user_id": {"type": "integer"},
                **_PAGING,
            },
            "required": ["course_id"],
        },
    ),
    Tool(
        name="moodle_get_completion",
        description="Activity completion of one user in a course.",
        inputSchema={
            "type": "object",
            "properties": {"course_id": {"type": "integer"}, "user_id": {"type": "integer"}},
            "required": ["course_id", "user_id"],
        },
    ),
    Tool(
        name="moodle_list_assignments",
        description="Assignments of a course with due dates and maximum grade.",
        inputSchema={
            "type": "object",
            "properties": {"course_id": {"type": "integer"}},
            "required": ["course_id"],
        },
    ),
    Tool(
        name="moodle_list_submissions",
        description=(
            "Submission status of every student for an assignment, including students who "
            "have submitted nothing (status 'new'). Needs course_id. Filter with status "
            "(submitted, draft, new) or user_id. With include_text=true and user_id it "
            "returns that one student's submitted text, file names and current grade."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "assignment_id": {"type": "integer"},
                "course_id": {"type": "integer"},
                "user_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["submitted", "draft", "new"]},
                "include_text": {"type": "boolean"},
                **_PAGING,
            },
            "required": ["assignment_id"],
        },
    ),
    Tool(
        name="moodle_grade_submission",
        description=(
            "Set a student's grade for an assignment, with optional feedback text. Needs "
            "the assignment's course_id. Grades must be between 0 and the assignment's "
            "maximum; assignments graded on a scale are refused (grade them in Moodle). "
            "Whether Moodle notifies the student follows the assignment's own "
            "notification setting."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "assignment_id": {"type": "integer"},
                "course_id": {"type": "integer"},
                "user_id": {"type": "integer"},
                "grade": {"type": "number", "minimum": 0},
                "feedback": {"type": "string"},
            },
            "required": ["assignment_id", "course_id", "user_id", "grade"],
        },
    ),
]

CALL_HANDLERS: dict[str, Callable[[Mapping[str, Any]], Awaitable[Any]]] = {
    "moodle_get_grades": moodle_get_grades,
    "moodle_get_completion": moodle_get_completion,
    "moodle_list_assignments": moodle_list_assignments,
    "moodle_list_submissions": moodle_list_submissions,
    "moodle_grade_submission": moodle_grade_submission,
}
