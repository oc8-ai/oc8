from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    """Drop every cached `connector`/`mcp_bridge` module: both names are shared
    by every capa, and sys.modules is keyed by name, not path."""
    for _stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge"} or n.startswith(("connector.", "mcp_bridge."))
    ]:
        del sys.modules[_stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

from mcp_bridge import client  # noqa: E402
from mcp_bridge.tools import grading  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()


def test_declares_the_five_grading_tools() -> None:
    names = {t.name for t in grading.TOOLS}
    assert names == {
        "moodle_get_grades",
        "moodle_get_completion",
        "moodle_list_assignments",
        "moodle_list_submissions",
        "moodle_grade_submission",
    }
    assert set(grading.CALL_HANDLERS) == names


@pytest.mark.asyncio
async def test_grades_for_one_user(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on(
        "gradereport_user_get_grade_items",
        {
            "usergrades": [
                {
                    "userid": 7,
                    "userfullname": "Ana Lima",
                    "gradeitems": [
                        {
                            "itemname": "Referat",
                            "itemtype": "mod",
                            "itemmodule": "assign",
                            "graderaw": 8.5,
                            "gradeformatted": "8,50",
                            "grademax": 10,
                            "percentageformatted": "85,00 %",
                            "feedback": "<p>Gut</p>",
                        },
                        {
                            "itemname": None,
                            "itemtype": "course",
                            "graderaw": None,
                            "gradeformatted": "-",
                            "grademax": 100,
                            "percentageformatted": "-",
                            "feedback": "",
                        },
                    ],
                }
            ]
        },
    )
    result = await grading.moodle_get_grades({"course_id": 5, "user_id": 7})
    assert fake.params("gradereport_user_get_grade_items") == {"courseid": 5, "userid": 7}
    row = result["items"][0]
    assert row["user_id"] == 7 and row["fullname"] == "Ana Lima"
    assert row["items"][0] == {
        "name": "Referat",
        "type": "assign",
        "grade": "8,50",
        "raw": 8.5,
        "max": 10,
        "percentage": "85,00 %",
        "feedback": "Gut",
    }
    assert row["items"][1]["name"] == "Course total"


@pytest.mark.asyncio
async def test_grades_for_the_whole_course_omit_userid(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on("gradereport_user_get_grade_items", {"usergrades": []})
    await grading.moodle_get_grades({"course_id": 5})
    assert fake.params("gradereport_user_get_grade_items") == {"courseid": 5}


@pytest.mark.asyncio
async def test_completion_states_are_named(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on(
        "core_completion_get_activities_completion_status",
        {
            "statuses": [
                {
                    "cmid": 41,
                    "modname": "assign",
                    "instance": 9,
                    "state": 2,
                    "timecompleted": 1767225600,
                },
                {"cmid": 42, "modname": "forum", "instance": 4, "state": 0, "timecompleted": 0},
            ]
        },
    )
    result = await grading.moodle_get_completion({"course_id": 5, "user_id": 7})
    assert fake.params("core_completion_get_activities_completion_status") == {
        "courseid": 5,
        "userid": 7,
    }
    assert result == [
        {
            "cmid": 41,
            "type": "assign",
            "instance": 9,
            "state": "complete_pass",
            "completed_at": "2026-01-01T00:00:00Z",
        },
        {"cmid": 42, "type": "forum", "instance": 4, "state": "incomplete", "completed_at": None},
    ]


@pytest.mark.asyncio
async def test_assignments_of_a_course(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on(
        "mod_assign_get_assignments",
        {
            "courses": [
                {
                    "id": 5,
                    "assignments": [
                        {
                            "id": 9,
                            "cmid": 41,
                            "name": "Referat",
                            "duedate": 1767225600,
                            "cutoffdate": 0,
                            "allowsubmissionsfromdate": 0,
                            "grade": 10,
                            "intro": "<p>Thema frei</p>",
                        },
                    ],
                }
            ]
        },
    )
    result = await grading.moodle_list_assignments({"course_id": 5})
    assert fake.params("mod_assign_get_assignments") == {
        "courseids": [5],
        "includenotenrolledcourses": 1,
    }
    assert result == [
        {
            "id": 9,
            "cmid": 41,
            "name": "Referat",
            "due": "2026-01-01T00:00:00Z",
            "cutoff": None,
            "opens": None,
            "max_grade": 10,
            "intro": "Thema frei",
        }
    ]


@pytest.mark.asyncio
async def test_assignments_of_a_course_the_user_cannot_see_raise_with_moodles_warning(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on(
        "mod_assign_get_assignments",
        {
            "courses": [],
            "warnings": [
                {
                    "item": "course",
                    "itemid": 2,
                    "warningcode": "2",
                    "message": "User is not enrolled or does not have requested capability",
                }
            ],
        },
    )
    with pytest.raises(
        ValueError,
        match="no assignments for course 2: User is not enrolled or does not have requested",
    ):
        await grading.moodle_list_assignments({"course_id": 2})


@pytest.mark.asyncio
async def test_assignments_without_warning_text_still_raise(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on("mod_assign_get_assignments", {"courses": []})
    with pytest.raises(ValueError, match=r"no assignments for course 2$"):
        await grading.moodle_list_assignments({"course_id": 2})


@pytest.mark.asyncio
async def test_a_course_without_assignments_is_a_legitimate_empty_list(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on("mod_assign_get_assignments", {"courses": [{"id": 5, "assignments": []}]})
    assert await grading.moodle_list_assignments({"course_id": 5}) == []


def _students_and_teacher() -> list[dict[str, Any]]:
    return [
        {"id": 1, "fullname": "Ana", "roles": [{"shortname": "student"}]},
        {"id": 2, "fullname": "Ben", "roles": [{"shortname": "student"}]},
        {"id": 3, "fullname": "Cem", "roles": [{"shortname": "student"}]},
        {"id": 9, "fullname": "Frau Weber", "roles": [{"shortname": "editingteacher"}]},
    ]


@pytest.mark.asyncio
async def test_submissions_include_students_without_a_row_and_never_teachers(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on(
        "mod_assign_get_submissions",
        {
            "assignments": [
                {
                    "assignmentid": 9,
                    "submissions": [
                        {
                            "userid": 1,
                            "status": "submitted",
                            "timemodified": 1767225600,
                            "gradingstatus": "notgraded",
                        },
                        {
                            "userid": 2,
                            "status": "draft",
                            "timemodified": 1767225600,
                            "gradingstatus": "notgraded",
                        },
                    ],
                }
            ]
        },
    )
    fake.on("core_enrol_get_enrolled_users", _students_and_teacher())
    result = await grading.moodle_list_submissions({"assignment_id": 9, "course_id": 5})
    assert fake.params("mod_assign_get_submissions") == {"assignmentids": [9]}
    rows = {r["user_id"]: r for r in result["items"]}
    assert set(rows) == {1, 2, 3}
    assert rows[1]["status"] == "submitted"
    assert rows[1]["submitted_at"] == "2026-01-01T00:00:00Z"
    assert rows[2]["status"] == "draft" and rows[2]["submitted_at"] is None
    assert rows[3] == {
        "user_id": 3,
        "fullname": "Cem",
        "status": "new",
        "submitted_at": None,
        "grading_status": "notgraded",
    }


@pytest.mark.asyncio
async def test_submissions_filter_by_status(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on(
        "mod_assign_get_submissions",
        {
            "assignments": [
                {
                    "assignmentid": 9,
                    "submissions": [
                        {
                            "userid": 1,
                            "status": "submitted",
                            "timemodified": 1,
                            "gradingstatus": "graded",
                        },
                    ],
                }
            ]
        },
    )
    fake.on("core_enrol_get_enrolled_users", _students_and_teacher())
    result = await grading.moodle_list_submissions(
        {"assignment_id": 9, "course_id": 5, "status": "new"}
    )
    assert [r["user_id"] for r in result["items"]] == [2, 3]


@pytest.mark.asyncio
async def test_list_mode_needs_the_course() -> None:
    with pytest.raises(ValueError, match="course_id is required"):
        await grading.moodle_list_submissions({"assignment_id": 9})


@pytest.mark.asyncio
async def test_submission_text_for_one_student(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on(
        "mod_assign_get_submission_status",
        {
            "lastattempt": {
                "submission": {
                    "status": "submitted",
                    "timemodified": 1767225600,
                    "plugins": [
                        {"type": "onlinetext", "editorfields": [{"text": "<p>Mein Referat</p>"}]},
                        {"type": "file", "fileareas": [{"files": [{"filename": "referat.pdf"}]}]},
                    ],
                }
            },
            "feedback": {"gradefordisplay": "8,50 / 10,00"},
        },
    )
    result = await grading.moodle_list_submissions(
        {"assignment_id": 9, "user_id": 1, "include_text": True}
    )
    assert fake.params("mod_assign_get_submission_status") == {"assignid": 9, "userid": 1}
    assert result == {
        "user_id": 1,
        "status": "submitted",
        "submitted_at": "2026-01-01T00:00:00Z",
        "text": "Mein Referat",
        "files": ["referat.pdf"],
        "grade": "8,50 / 10,00",
    }


@pytest.mark.asyncio
async def test_include_text_needs_one_user() -> None:
    with pytest.raises(ValueError, match="include_text needs user_id"):
        await grading.moodle_list_submissions(
            {"assignment_id": 9, "course_id": 5, "include_text": True}
        )


def _assignment(max_grade: float) -> dict[str, Any]:
    return {"courses": [{"id": 5, "assignments": [{"id": 9, "grade": max_grade}]}]}


@pytest.mark.asyncio
async def test_grade_submission_writes_grade_and_feedback(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on("mod_assign_get_assignments", _assignment(10))
    fake.on("mod_assign_save_grade", None)
    result = await grading.moodle_grade_submission(
        {
            "assignment_id": 9,
            "course_id": 5,
            "user_id": 1,
            "grade": 8.5,
            "feedback": "<p>Gute Gliederung.</p>",
        }
    )
    assert fake.params("mod_assign_get_assignments") == {
        "courseids": [5],
        "includenotenrolledcourses": 1,
    }
    assert fake.params("mod_assign_save_grade") == {
        "assignmentid": 9,
        "userid": 1,
        "grade": 8.5,
        "attemptnumber": -1,
        "addattempt": 0,
        "workflowstate": "",
        "applytoall": 1,
        "plugindata": {
            "assignfeedbackcomments_editor": {"text": "<p>Gute Gliederung.</p>", "format": 1}
        },
    }
    assert result == {"graded": True, "assignment_id": 9, "user_id": 1, "grade": 8.5}


@pytest.mark.asyncio
async def test_plain_feedback_keeps_its_line_breaks_as_html(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on("mod_assign_get_assignments", _assignment(10))
    fake.on("mod_assign_save_grade", None)
    await grading.moodle_grade_submission(
        {
            "assignment_id": 9,
            "course_id": 5,
            "user_id": 1,
            "grade": 8,
            "feedback": "Gut.\nQuellen & Zitate fehlen.",
        }
    )
    editor = fake.params("mod_assign_save_grade")["plugindata"]["assignfeedbackcomments_editor"]
    assert editor["text"] == "Gut.<br>Quellen &amp; Zitate fehlen."


@pytest.mark.asyncio
async def test_grade_without_feedback_sends_no_plugindata(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on("mod_assign_get_assignments", _assignment(10))
    fake.on("mod_assign_save_grade", None)
    await grading.moodle_grade_submission(
        {"assignment_id": 9, "course_id": 5, "user_id": 1, "grade": 7}
    )
    assert "plugindata" not in fake.params("mod_assign_save_grade")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("grade", "message"),
    [
        (float("nan"), "not a valid grade"),
        (float("inf"), "not a valid grade"),
        (-1, "cannot be negative"),
    ],
)
async def test_invalid_grades_are_refused_before_any_call(
    fake_moodle: Callable[[Any], Any], grade: float, message: str
) -> None:
    fake = fake_moodle(client)
    with pytest.raises(ValueError, match=message):
        await grading.moodle_grade_submission(
            {"assignment_id": 9, "course_id": 5, "user_id": 1, "grade": grade}
        )
    assert fake.calls == []


@pytest.mark.asyncio
async def test_grade_submission_needs_the_course() -> None:
    with pytest.raises(ValueError, match="course_id is required"):
        await grading.moodle_grade_submission({"assignment_id": 9, "user_id": 1, "grade": 5})


@pytest.mark.asyncio
async def test_a_grade_above_the_maximum_is_refused(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on("mod_assign_get_assignments", _assignment(10))
    with pytest.raises(ValueError, match="Grade 12 is above the maximum 10 for this assignment"):
        await grading.moodle_grade_submission(
            {"assignment_id": 9, "course_id": 5, "user_id": 1, "grade": 12}
        )
    assert [f for f, _ in fake.calls] == ["mod_assign_get_assignments"]


@pytest.mark.asyncio
async def test_a_scale_graded_assignment_is_refused(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on("mod_assign_get_assignments", _assignment(-3))
    with pytest.raises(ValueError, match="uses a scale; grade it in Moodle"):
        await grading.moodle_grade_submission(
            {"assignment_id": 9, "course_id": 5, "user_id": 1, "grade": 2}
        )
    assert [f for f, _ in fake.calls] == ["mod_assign_get_assignments"]


@pytest.mark.asyncio
async def test_an_assignment_outside_the_course_is_refused(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on("mod_assign_get_assignments", {"courses": [{"id": 5, "assignments": []}]})
    with pytest.raises(ValueError, match="Assignment 9 was not found in course 5"):
        await grading.moodle_grade_submission(
            {"assignment_id": 9, "course_id": 5, "user_id": 1, "grade": 2}
        )


@pytest.mark.asyncio
async def test_missing_assignment_raises_instead_of_listing_everyone_as_new(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on("mod_assign_get_submissions", {"assignments": []})
    fake.on("core_enrol_get_enrolled_users", _students_and_teacher())
    with pytest.raises(ValueError, match="no submissions for assignment 9"):
        await grading.moodle_list_submissions({"assignment_id": 9, "course_id": 5})


@pytest.mark.asyncio
async def test_moodle_warning_is_surfaced_as_an_error(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on(
        "mod_assign_get_submissions",
        {
            "assignments": [],
            "warnings": [{"item": "assignment", "itemid": 9, "message": "No access"}],
        },
    )
    fake.on("core_enrol_get_enrolled_users", _students_and_teacher())
    with pytest.raises(ValueError, match="assignment 9: No access"):
        await grading.moodle_list_submissions({"assignment_id": 9, "course_id": 5})
