"""Which Moodle web service functions each tool calls.

One table for three readers: `moodle_site_info` names the functions the
oc8 external service is missing (and which tool each one disables), the
docs page lists them for an admin to copy, and the manifest test checks
that every exposed tool is listed here.
"""

from __future__ import annotations

TOOL_FUNCTIONS: dict[str, tuple[str, ...]] = {
    "moodle_site_info": ("core_webservice_get_site_info",),
    "moodle_search_courses": ("core_course_search_courses", "core_course_get_courses_by_field"),
    "moodle_get_course_contents": ("core_course_get_contents",),
    "moodle_create_course": ("core_course_create_courses",),
    "moodle_update_course": ("core_course_update_courses",),
    "moodle_list_participants": ("core_enrol_get_enrolled_users",),
    "moodle_find_users": ("core_user_get_users_by_field",),
    "moodle_create_user": ("core_user_create_users",),
    "moodle_update_user": ("core_user_update_users",),
    "moodle_enrol_user": ("enrol_manual_enrol_users",),
    "moodle_unenrol_user": ("enrol_manual_unenrol_users",),
    "moodle_get_grades": ("gradereport_user_get_grade_items",),
    "moodle_get_completion": ("core_completion_get_activities_completion_status",),
    "moodle_list_assignments": ("mod_assign_get_assignments",),
    "moodle_list_submissions": (
        "mod_assign_get_submissions",
        "mod_assign_get_submission_status",
        "core_enrol_get_enrolled_users",
    ),
    "moodle_grade_submission": ("mod_assign_get_assignments", "mod_assign_save_grade"),
    "moodle_list_forum_discussions": (
        "mod_forum_get_forum_discussions",
        "mod_forum_get_discussion_posts",
    ),
    "moodle_send_message": ("core_message_send_instant_messages",),
    "moodle_post_to_forum": ("mod_forum_add_discussion", "mod_forum_add_discussion_post"),
}


def all_functions() -> list[str]:
    return sorted({name for names in TOOL_FUNCTIONS.values() for name in names})
