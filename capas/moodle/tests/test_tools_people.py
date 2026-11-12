from __future__ import annotations

import sys
import time
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
from mcp_bridge.tools import people  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()

DAY = 86400
#: Only active enrolments, and only the fields an agent needs.
ACTIVE_ONLY = {
    "courseid": 5,
    "options": [
        {"name": "onlyactive", "value": 1},
        {
            "name": "userfields",
            "value": "id,fullname,email,username,roles,groups,lastaccess,lastcourseaccess,"
            "suspended",
        },
    ],
}


def _user(uid: int, role: str, last_course_access: int) -> dict[str, Any]:
    return {
        "id": uid,
        "fullname": f"User {uid}",
        "email": f"u{uid}@example.org",
        "roles": [{"roleid": 5, "shortname": role}],
        "lastcourseaccess": last_course_access,
        "phone1": "secret",
    }


def test_declares_the_six_people_tools() -> None:
    names = {t.name for t in people.TOOLS}
    assert names == {
        "moodle_list_participants",
        "moodle_find_users",
        "moodle_create_user",
        "moodle_update_user",
        "moodle_enrol_user",
        "moodle_unenrol_user",
    }
    assert set(people.CALL_HANDLERS) == names


@pytest.mark.asyncio
async def test_participants_filter_by_role_and_inactivity_and_drop_private_fields(
    fake_moodle: Callable[[Any], Any],
) -> None:
    now = int(time.time())
    fake = fake_moodle(client)
    fake.on(
        "core_enrol_get_enrolled_users",
        [
            _user(1, "student", now - DAY),
            _user(2, "student", now - 30 * DAY),
            _user(3, "student", 0),
            _user(4, "editingteacher", now - 40 * DAY),
        ],
    )
    result = await people.moodle_list_participants(
        {"course_id": 5, "role": "student", "only_inactive_days": 14}
    )
    assert fake.params("core_enrol_get_enrolled_users") == ACTIVE_ONLY
    assert [u["id"] for u in result["items"]] == [2, 3]
    assert all("phone1" not in u for u in result["items"])
    assert result["items"][1]["last_course_access"] is None


@pytest.mark.asyncio
async def test_participants_beyond_the_limit_are_capped(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on("core_enrol_get_enrolled_users", [_user(i, "student", 0) for i in range(120)])
    result = await people.moodle_list_participants({"course_id": 5})
    assert len(result["items"]) == 50
    assert result["total"] == 120 and result["truncated"] is True


@pytest.mark.asyncio
async def test_find_users_demands_exactly_one_key() -> None:
    with pytest.raises(ValueError, match="exactly one of: email, username, id, idnumber"):
        await people.moodle_find_users({"email": "a@b.c", "username": "ana"})
    with pytest.raises(ValueError, match="exactly one of"):
        await people.moodle_find_users({})


@pytest.mark.asyncio
async def test_find_users_by_email(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on("core_user_get_users_by_field", [{"id": 7, "fullname": "Ana", "address": "x"}])
    result = await people.moodle_find_users({"email": "ana@example.org"})
    assert fake.params("core_user_get_users_by_field") == {
        "field": "email",
        "values": ["ana@example.org"],
    }
    assert result == [{"id": 7, "fullname": "Ana"}]


@pytest.mark.asyncio
async def test_create_user_lets_moodle_mail_a_password_for_manual_accounts(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on("core_user_create_users", [{"id": 9, "username": "ana"}])
    result = await people.moodle_create_user(
        {"username": "Ana", "firstname": "Ana", "lastname": "Lima", "email": "ana@example.org"}
    )
    assert fake.params("core_user_create_users") == {
        "users": [
            {
                "username": "ana",
                "firstname": "Ana",
                "lastname": "Lima",
                "email": "ana@example.org",
                "auth": "manual",
                "createpassword": 1,
            }
        ]
    }
    assert result == {"created": [{"id": 9, "username": "ana"}]}


@pytest.mark.asyncio
async def test_create_user_with_sso_auth_sends_no_password(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on("core_user_create_users", [{"id": 9, "username": "ana"}])
    await people.moodle_create_user(
        {
            "username": "ana",
            "firstname": "Ana",
            "lastname": "Lima",
            "email": "ana@example.org",
            "auth": "oauth2",
        }
    )
    assert "createpassword" not in fake.params("core_user_create_users")["users"][0]


@pytest.mark.asyncio
async def test_update_user_maps_suspended_and_needs_a_change(
    fake_moodle: Callable[[Any], Any],
) -> None:
    with pytest.raises(ValueError, match="Nothing to change"):
        await people.moodle_update_user({"user_id": 7})
    fake = fake_moodle(client)
    fake.on("core_user_update_users", None)
    result = await people.moodle_update_user({"user_id": 7, "suspended": True})
    assert fake.params("core_user_update_users") == {"users": [{"id": 7, "suspended": 1}]}
    assert result == {"updated": 7}


@pytest.mark.asyncio
async def test_update_user_raises_when_moodle_refuses_it_with_a_warning(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on(
        "core_user_update_users",
        {
            "warnings": [
                {
                    "item": "user",
                    "itemid": 7,
                    "warningcode": "usernotupdatedemailexists",
                    "message": "Email address already exists",
                },
                {"item": "user", "warningcode": "x", "message": "Second reason"},
            ]
        },
    )
    with pytest.raises(ValueError, match="Email address already exists; Second reason"):
        await people.moodle_update_user({"user_id": 7, "email": "dup@example.org"})


@pytest.mark.asyncio
async def test_update_user_ignores_a_warning_about_another_user(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on(
        "core_user_update_users",
        {"warnings": [{"item": "user", "itemid": 8, "warningcode": "x", "message": "Other"}]},
    )
    assert await people.moodle_update_user({"user_id": 7, "firstname": "Ana"}) == {"updated": 7}


@pytest.mark.asyncio
async def test_enrol_maps_role_names_to_moodle_default_ids(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on("enrol_manual_enrol_users", None)
    result = await people.moodle_enrol_user({"course_id": 5, "user_id": 7, "role": "teacher"})
    assert fake.params("enrol_manual_enrol_users") == {
        "enrolments": [{"roleid": 4, "userid": 7, "courseid": 5}],
    }
    assert result == {"enrolled": True, "course_id": 5, "user_id": 7, "role_id": 4}


@pytest.mark.asyncio
async def test_enrol_defaults_to_student_and_honours_an_explicit_role_id(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on("enrol_manual_enrol_users", None)
    await people.moodle_enrol_user({"course_id": 5, "user_id": 7})
    assert fake.calls[-1][1]["enrolments"][0]["roleid"] == 5
    await people.moodle_enrol_user({"course_id": 5, "user_id": 7, "role_id": 11})
    assert fake.calls[-1][1]["enrolments"][0]["roleid"] == 11


@pytest.mark.asyncio
async def test_enrol_refuses_an_unknown_role_name() -> None:
    with pytest.raises(ValueError, match="Unknown role 'manager'"):
        await people.moodle_enrol_user({"course_id": 5, "user_id": 7, "role": "manager"})


@pytest.mark.asyncio
async def test_unenrol(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on("enrol_manual_unenrol_users", None)
    result = await people.moodle_unenrol_user({"course_id": 5, "user_id": 7})
    assert fake.params("enrol_manual_unenrol_users") == {
        "enrolments": [{"userid": 7, "courseid": 5}],
    }
    assert result == {"unenrolled": True, "course_id": 5, "user_id": 7}
