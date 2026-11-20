from __future__ import annotations

import sys
from pathlib import Path

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

from mcp_bridge import shaping  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()


def test_plain_text_strips_tags_keeps_line_breaks_and_unescapes() -> None:
    assert shaping.plain_text("<p>Hallo&nbsp;Ana</p><p>Zeile <b>zwei</b><br>drei</p>") == (
        "Hallo\xa0Ana\nZeile zwei\ndrei"
    )


def test_plain_text_caps_long_text_with_a_visible_mark() -> None:
    text = shaping.plain_text("x" * 5000)
    assert len(text) == shaping.MAX_TEXT + len(shaping.TRUNCATED_MARK)
    assert text.endswith(shaping.TRUNCATED_MARK)
    assert shaping.plain_text(None) == ""


def test_as_html_escapes_and_keeps_line_breaks() -> None:
    assert shaping.as_html("a < b\nzweite") == "a &lt; b<br>zweite"


def test_page_caps_reports_total_and_truncation() -> None:
    items = list(range(120))
    first = shaping.page(items, {})
    assert first["items"] == list(range(50))
    assert (first["total"], first["offset"], first["truncated"]) == (120, 0, True)
    last = shaping.page(items, {"limit": 50, "offset": 100})
    assert last["items"] == list(range(100, 120))
    assert last["truncated"] is False


def test_limits_are_clamped() -> None:
    assert shaping.clamp_limit(10_000) == 200
    assert shaping.clamp_limit(0) == 1
    assert shaping.clamp_limit("x") == 50
    assert shaping.clamp_offset(-5) == 0


def test_iso_and_to_timestamp_round_trip_in_utc() -> None:
    assert shaping.iso(0) is None
    assert shaping.iso(None) is None
    assert shaping.iso(1767225600) == "2026-01-01T00:00:00Z"
    assert shaping.to_timestamp("2026-01-01") == 1767225600
    assert shaping.to_timestamp("2026-01-01T00:00:00Z") == 1767225600
    assert shaping.to_timestamp(1767225600) == 1767225600
    with pytest.raises(ValueError):
        shaping.to_timestamp("next monday")


def test_pick_one_demands_exactly_one() -> None:
    assert shaping.pick_one({"email": "a@b.c"}, ("email", "id")) == ("email", "a@b.c")
    with pytest.raises(ValueError, match="exactly one of: email, id"):
        shaping.pick_one({"email": "a@b.c", "id": 3}, ("email", "id"))
    with pytest.raises(ValueError, match="exactly one of: email, id"):
        shaping.pick_one({"email": ""}, ("email", "id"))


def test_shape_user_keeps_only_the_allowed_fields() -> None:
    raw = {
        "id": 7,
        "fullname": "Ana Lima",
        "email": "ana@example.org",
        "username": "ana",
        "suspended": 0,
        "lastaccess": 1767225600,
        "lastcourseaccess": 0,
        "roles": [{"roleid": 5, "shortname": "student"}],
        "groups": [{"id": 1, "name": "Gruppe A"}],
        "address": "Hauptstr. 1",
        "phone1": "0123",
        "description": "<p>bio</p>",
        "customfields": [{"shortname": "dob", "value": "2001-01-01"}],
        "profileimageurl": "https://lms.example.org/pic.png",
    }
    assert shaping.shape_user(raw) == {
        "id": 7,
        "fullname": "Ana Lima",
        "email": "ana@example.org",
        "username": "ana",
        "suspended": False,
        "roles": ["student"],
        "groups": ["Gruppe A"],
        "last_access": "2026-01-01T00:00:00Z",
        "last_course_access": None,
    }
    assert shaping.has_role(raw, "student") and not shaping.has_role(raw, "teacher")


def test_shape_course_converts_dates_and_shortens_the_summary() -> None:
    course = shaping.shape_course(
        {
            "id": 5,
            "fullname": "Biologie 10a",
            "shortname": "BIO10A",
            "categoryid": 2,
            "visible": 1,
            "startdate": 1767225600,
            "enddate": 0,
            "summary": "<p>Zellen</p>",
            "summaryfiles": [],
            "contacts": [{"fullname": "x"}],
        }
    )
    assert course == {
        "id": 5,
        "fullname": "Biologie 10a",
        "shortname": "BIO10A",
        "category_id": 2,
        "visible": True,
        "start": "2026-01-01T00:00:00Z",
        "end": None,
        "summary": "Zellen",
    }
