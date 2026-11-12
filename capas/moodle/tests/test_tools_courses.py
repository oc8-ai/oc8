from __future__ import annotations

import sys
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

from collections.abc import Callable  # noqa: E402

from mcp_bridge import client  # noqa: E402
from mcp_bridge.catalog import all_functions  # noqa: E402
from mcp_bridge.tools import courses  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()

ALL_FUNCTIONS = all_functions()


def test_declares_the_five_course_tools() -> None:
    names = {t.name for t in courses.TOOLS}
    assert names == {
        "moodle_site_info",
        "moodle_search_courses",
        "moodle_get_course_contents",
        "moodle_create_course",
        "moodle_update_course",
    }
    assert set(courses.CALL_HANDLERS) == names


@pytest.mark.asyncio
async def test_site_info_names_missing_functions_and_old_versions(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on(
        "core_webservice_get_site_info",
        {
            "sitename": "Schule",
            "siteurl": "https://lms.example.org",
            "release": "4.0.5",
            "version": "2022041905",
            "userid": 3,
            "fullname": "oc8 Service",
            "username": "oc8",
            "functions": [
                {"name": "core_webservice_get_site_info"},
                {"name": "core_course_get_contents"},
            ],
        },
    )
    info = await courses.moodle_site_info({})
    assert info["site_name"] == "Schule"
    assert info["service_user"] == {"id": 3, "fullname": "oc8 Service", "username": "oc8"}
    missing = {(m["function"], m["tool"]) for m in info["missing_functions"]}
    assert ("mod_assign_save_grade", "moodle_grade_submission") in missing
    assert all(m["function"] != "core_course_get_contents" for m in info["missing_functions"])
    assert "older than 4.1" in info["version_warning"]


@pytest.mark.asyncio
async def test_site_info_on_a_current_site_has_no_warning(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on(
        "core_webservice_get_site_info",
        {"version": "2024100700", "functions": [{"name": n} for n in ALL_FUNCTIONS]},
    )
    info = await courses.moodle_site_info({})
    assert info["missing_functions"] == []
    assert "version_warning" not in info


@pytest.mark.asyncio
async def test_search_by_query_uses_the_search_function(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on(
        "core_course_search_courses",
        {
            "total": 1,
            "courses": [
                {
                    "id": 5,
                    "fullname": "Biologie 10a",
                    "shortname": "BIO10A",
                    "categoryid": 2,
                    "startdate": 0,
                    "enddate": 0,
                    "summary": "",
                }
            ],
        },
    )
    result = await courses.moodle_search_courses({"query": "Bio"})
    assert fake.params("core_course_search_courses") == {
        "criterianame": "search",
        "criteriavalue": "Bio",
        "page": 0,
        "perpage": 200,
    }
    assert result["items"][0]["fullname"] == "Biologie 10a"
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_search_by_field_uses_get_courses_by_field(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on("core_course_get_courses_by_field", {"courses": [{"id": 5, "shortname": "BIO10A"}]})
    await courses.moodle_search_courses({"shortname": "BIO10A"})
    assert fake.params("core_course_get_courses_by_field") == {
        "field": "shortname",
        "value": "BIO10A",
    }


@pytest.mark.asyncio
async def test_search_refuses_query_and_field_together() -> None:
    with pytest.raises(ValueError, match="exactly one of"):
        await courses.moodle_search_courses({"query": "Bio", "id": 5})


@pytest.mark.asyncio
async def test_course_contents_expose_the_instance_ids_other_tools_need(
    fake_moodle: Callable[[Any], Any],
) -> None:
    fake = fake_moodle(client)
    fake.on(
        "core_course_get_contents",
        [
            {
                "section": 1,
                "name": "Woche 1",
                "visible": 1,
                "modules": [
                    {"id": 41, "instance": 9, "modname": "assign", "name": "Referat", "visible": 1},
                    {"id": 42, "instance": 4, "modname": "forum", "name": "Fragen", "visible": 0},
                ],
            },
        ],
    )
    sections = await courses.moodle_get_course_contents({"course_id": 5})
    assert fake.params("core_course_get_contents") == {"courseid": 5}
    assert sections[0]["modules"] == [
        {"cmid": 41, "instance": 9, "type": "assign", "name": "Referat", "visible": True},
        {"cmid": 42, "instance": 4, "type": "forum", "name": "Fragen", "visible": False},
    ]


@pytest.mark.asyncio
async def test_create_course_maps_fields_and_dates(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on("core_course_create_courses", [{"id": 12, "shortname": "CHE11"}])
    result = await courses.moodle_create_course(
        {
            "fullname": "Chemie 11",
            "shortname": "CHE11",
            "category_id": 2,
            "start_date": "2026-01-01",
            "visible": False,
        }
    )
    assert fake.params("core_course_create_courses") == {
        "courses": [
            {
                "fullname": "Chemie 11",
                "shortname": "CHE11",
                "categoryid": 2,
                "visible": 0,
                "startdate": 1767225600,
            }
        ]
    }
    assert result == {"created": [{"id": 12, "shortname": "CHE11"}]}


@pytest.mark.asyncio
async def test_update_course_needs_something_to_change() -> None:
    with pytest.raises(ValueError, match="Nothing to change"):
        await courses.moodle_update_course({"course_id": 5})


@pytest.mark.asyncio
async def test_update_course_sends_only_given_fields(fake_moodle: Callable[[Any], Any]) -> None:
    fake = fake_moodle(client)
    fake.on("core_course_update_courses", {"warnings": []})
    result = await courses.moodle_update_course({"course_id": 5, "fullname": "Bio 10a (neu)"})
    assert fake.params("core_course_update_courses") == {
        "courses": [{"id": 5, "fullname": "Bio 10a (neu)"}],
    }
    assert result == {"updated": 5, "warnings": []}
