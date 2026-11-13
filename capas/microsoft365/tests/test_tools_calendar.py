from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    """Drop every cached `connector`/`mcp_bridge` module from sys.modules.

    Both names are generic -- after the package restructure every plugin ships
    a package called one of them (design §5.2) -- and sys.modules is keyed by
    NAME, not by path. Called SYMMETRICALLY around the import below: before,
    so a sibling plugin's cached copy cannot answer it; after, so nothing
    generic is left cached for anyone else. The "after" half is the
    load-bearing one -- `loader.import_entry_point` (Task 6's collision fix)
    only evicts modules IT ITSELF introduced, so a name left cached here makes
    a later `find_plugin`/`load_plugin` for gdrive_source or google_workspace
    silently hand back THIS plugin's code. Verified live. The module objects
    bound by the import block stay valid across the eviction.
    """
    for _stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge"} or n.startswith(("connector.", "mcp_bridge."))
    ]:
        del sys.modules[_stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

from mcp_bridge import graph  # noqa: E402
from mcp_bridge._user import DEFAULT_USER_ENV  # noqa: E402
from mcp_bridge.tools.calendar import CALL_HANDLERS, TOOLS  # noqa: E402

sys.path.remove(str(PLUGIN_ROOT))
_evict()


def test_declares_the_seven_calendar_tools() -> None:
    names = {t.name for t in TOOLS}
    assert names == {
        "calendar_list_events",
        "calendar_get_event",
        "calendar_create_event",
        "calendar_update_event",
        "calendar_cancel_event",
        "calendar_respond_to_invite",
        "calendar_check_free_busy",
    }
    assert set(CALL_HANDLERS) == names


async def test_create_event_posts_start_end_and_attendees(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_post(path: str, json: dict[str, Any]) -> dict[str, Any]:
        captured["path"] = path
        captured["json"] = json
        return {"id": "e1"}

    monkeypatch.setattr(graph, "post", fake_post)
    monkeypatch.delenv(DEFAULT_USER_ENV, raising=False)
    result = await CALL_HANDLERS["calendar_create_event"](
        {
            "subject": "Standup",
            "start": "2026-08-20T09:00:00",
            "end": "2026-08-20T09:30:00",
            "attendees": ["a@example.com"],
            "userId": "sina@contoso.com",
        }
    )
    # NOT `/me/events`: app-only auth has no signed-in user and Graph rejects
    # `/me` outright (whole-branch review C1).
    assert captured["path"] == "/users/sina@contoso.com/events"
    assert captured["json"]["subject"] == "Standup"
    assert captured["json"]["start"]["dateTime"] == "2026-08-20T09:00:00"
    assert captured["json"]["attendees"][0]["emailAddress"]["address"] == "a@example.com"
    assert result == {"id": "e1"}


async def test_list_events_with_a_range_uses_calendar_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        captured["path"] = path
        captured["params"] = params or {}
        return {"value": [{"id": "e1"}]}

    monkeypatch.setattr(graph, "get", fake_get)
    monkeypatch.delenv(DEFAULT_USER_ENV, raising=False)
    result = await CALL_HANDLERS["calendar_list_events"](
        {
            "start": "2026-08-20T00:00:00",
            "end": "2026-08-27T00:00:00",
            "userId": "sina@contoso.com",
        }
    )
    # `/events` silently ignores startDateTime/endDateTime; only `/calendarView`
    # honours them.
    assert captured["path"] == "/users/sina@contoso.com/calendarView"
    assert captured["params"]["startDateTime"] == "2026-08-20T00:00:00"
    assert captured["params"]["endDateTime"] == "2026-08-27T00:00:00"
    assert result == [{"id": "e1"}]


async def _list_with(monkeypatch: pytest.MonkeyPatch, args: dict[str, Any]) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        captured["path"] = path
        captured["params"] = params or {}
        return {"value": []}

    monkeypatch.setattr(graph, "get", fake_get)
    monkeypatch.delenv(DEFAULT_USER_ENV, raising=False)
    await CALL_HANDLERS["calendar_list_events"]({**args, "userId": "sina@contoso.com"})
    return captured


async def test_list_events_with_only_a_start_reads_the_week_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _list_with(monkeypatch, {"start": "2026-08-20T09:00:00"})
    assert captured["path"] == "/users/sina@contoso.com/calendarView"
    assert captured["params"]["startDateTime"] == "2026-08-20T09:00:00"
    assert captured["params"]["endDateTime"] == "2026-08-27T09:00:00"


async def test_list_events_with_only_a_start_keeps_its_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _list_with(monkeypatch, {"start": "2026-08-20T09:00:00Z"})
    assert captured["params"]["startDateTime"] == "2026-08-20T09:00:00Z"
    assert captured["params"]["endDateTime"] == "2026-08-27T09:00:00+00:00"


async def test_list_events_with_only_an_end_reads_from_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    end = (dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=3)).replace(microsecond=0)
    captured = await _list_with(monkeypatch, {"end": end.isoformat()})
    assert captured["path"] == "/users/sina@contoso.com/calendarView"
    assert captured["params"]["endDateTime"] == end.isoformat()
    start = dt.datetime.fromisoformat(captured["params"]["startDateTime"])
    assert abs((dt.datetime.now(tz=dt.UTC) - start).total_seconds()) < 60


async def test_list_events_with_only_a_past_end_reads_the_week_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _list_with(monkeypatch, {"end": "2020-03-08T00:00:00"})
    assert captured["params"]["startDateTime"] == "2020-03-01T00:00:00"
    assert captured["params"]["endDateTime"] == "2020-03-08T00:00:00"


async def test_list_events_refuses_a_bound_that_is_not_a_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="ISO 8601"):
        await _list_with(monkeypatch, {"start": "next tuesday"})


async def test_list_events_without_a_range_keeps_the_events_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        captured["path"] = path
        captured["params"] = params or {}
        return {"value": []}

    monkeypatch.setattr(graph, "get", fake_get)
    monkeypatch.delenv(DEFAULT_USER_ENV, raising=False)
    await CALL_HANDLERS["calendar_list_events"]({"userId": "sina@contoso.com"})
    assert captured["path"] == "/users/sina@contoso.com/events"
    assert "startDateTime" not in captured["params"]
    assert "endDateTime" not in captured["params"]


async def test_respond_to_invite_posts_the_right_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_post(path: str, json: dict[str, Any]) -> dict[str, Any]:
        captured["path"] = path
        return {}

    monkeypatch.setattr(graph, "post", fake_post)
    monkeypatch.setenv(DEFAULT_USER_ENV, "info@contoso.com")
    # No userId in the args: the connection's configured default mailbox is
    # what a tenant with one mailbox actually relies on.
    await CALL_HANDLERS["calendar_respond_to_invite"]({"eventId": "e1", "response": "accept"})
    assert captured["path"] == "/users/info@contoso.com/events/e1/accept"


def test_every_calendar_tool_accepts_an_optional_user_id() -> None:
    for tool in TOOLS:
        assert "userId" in tool.input_schema["properties"], tool.name
        assert "userId" not in tool.input_schema.get("required", []), tool.name


async def test_no_calendar_handler_still_builds_a_me_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def record(path: str, *a: object, **k: object) -> dict[str, Any]:
        seen.append(path)
        return {"value": []}

    for verb in ("get", "post", "patch", "delete"):
        monkeypatch.setattr(graph, verb, record)
    monkeypatch.setenv(DEFAULT_USER_ENV, "info@contoso.com")

    args = {
        "eventId": "e1",
        "subject": "s",
        "start": "2026-08-20T09:00:00",
        "end": "2026-08-20T09:30:00",
        "response": "accept",
        "attendees": ["a@example.com"],
    }
    for name, handler in CALL_HANDLERS.items():
        await handler(dict(args))
        assert seen, name
    assert not any(p.startswith("/me") for p in seen), seen
    assert all(p.startswith("/users/info@contoso.com/") for p in seen), seen
