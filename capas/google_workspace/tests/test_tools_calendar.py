from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _evict() -> None:
    """Drop every cached `connector`/`mcp_bridge` module from sys.modules.

    Both names are generic -- after the package restructure every plugin ships
    a package called one of them (design §5.2) -- and sys.modules is keyed by
    NAME, not by path. Called SYMMETRICALLY on fixture setup AND teardown:
    before, so a sibling plugin's cached copy cannot answer our import; after,
    so nothing generic is left cached for anyone else. The teardown half is
    the load-bearing one -- `loader.import_entry_point` (Task 6's collision
    fix) only evicts modules IT ITSELF introduced, so a name left cached here
    makes a later `find_plugin`/`load_plugin` for gdrive_source or
    microsoft365 silently hand back THIS plugin's code. Verified live.
    """
    for _stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge", "events"}
        or n.startswith(("connector.", "mcp_bridge.", "events."))
    ]:
        del sys.modules[_stale]


@pytest.fixture(autouse=True)
def _plugin_path() -> Iterator[None]:
    """Make THIS plugin's packages the ones that resolve, for each test.

    Function-scoped and autouse, per Task 7's convention: every plugin import
    in this file sits INSIDE a test body and resolves at execution time, long
    after any collection-time module-top prelude would have run.

    Before the move this file had NO path handling at all -- it worked only
    because a sibling test module in the same directory had already inserted
    the plugin root at collection time and left it there. That is exactly the
    accident this fixture replaces: each file now makes its own plugin
    resolvable, and leaves nothing behind for anyone else.
    """
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


pytestmark = pytest.mark.asyncio


async def test_calendar_create_event_posts_to_the_resolved_mailbox_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import calendar as tools_calendar

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"id": "evt1"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    result = await tools_calendar.calendar_create_event(
        {
            "mailbox": "a@company.com",
            "summary": "Sync",
            "start": "2026-08-20T10:00:00Z",
            "end": "2026-08-20T10:30:00Z",
        }
    )
    assert captured["path"] == "/calendar/v3/calendars/a@company.com/events"
    assert result == {"id": "evt1"}


async def test_calendar_create_meet_link_sets_conference_data_and_the_required_query_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google Calendar SILENTLY IGNORES `conferenceData` on events.insert/patch
    unless the request also carries `conferenceDataVersion=1` as a QUERY
    PARAMETER -- no error, no warning, you just get an ordinary event with no
    `hangoutLink`. This is exactly the class of defect §9's mock-discipline
    lesson warns about: a mock that returns a `hangoutLink` regardless of
    whether the real API would have honored the request makes this test pass
    while the feature is dead in production. Assert on the query param, not
    just the body."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import calendar as tools_calendar

    captured_body: dict[str, Any] = {}
    captured_params: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured_body.update(json.loads(request.content))
        captured_params.update(dict(request.url.params))
        return httpx.Response(
            200, json={"id": "evt2", "hangoutLink": "https://meet.google.com/abc-defg-hij"}
        )

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    result = await tools_calendar.calendar_create_meet_link(
        {
            "mailbox": "a@company.com",
            "summary": "Standup",
            "start": "2026-08-20T10:00:00Z",
            "end": "2026-08-20T10:15:00Z",
        }
    )
    assert (
        captured_body["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"]
        == "hangoutsMeet"
    )
    assert captured_params["conferenceDataVersion"] == "1"
    assert result["hangoutLink"] == "https://meet.google.com/abc-defg-hij"


async def test_calendar_respond_to_invite_preserves_every_other_attendee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calendar's events.patch treats `attendees` as an array-valued field
    REPLACED WHOLESALE, not merged -- PATCHing a one-element array silently
    removes every other attendee from the meeting, a destructive change to
    other people's calendars on a tool already classified in `outward_tools`.
    `self` is also output-only on EventAttendee and cannot select which
    attendee to modify. The correct pattern is read-modify-write: GET the
    event, find the attendee whose `email` matches the acting mailbox, set
    only that entry's `responseStatus`, and PATCH back the COMPLETE list.

    The implementation calls BOTH `resolve_mailbox_token` (for the bearer
    token) AND `resolve_mailbox_address` (for the real address to match
    against attendees) -- `GOOGLE_DELEGATED_MAILBOXES` must be set for the
    real `resolve_mailbox_address` to resolve "a@company.com" instead of
    raising "not one of this connection's authorized delegated mailboxes"."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import calendar as tools_calendar

    monkeypatch.setenv("GOOGLE_DELEGATED_MAILBOXES", "a@company.com")

    get_response = {
        "id": "evt3",
        "attendees": [
            {"email": "a@company.com", "responseStatus": "needsAction"},
            {"email": "bob@customer.com", "responseStatus": "needsAction"},
            {"email": "carol@customer.com", "responseStatus": "accepted"},
        ],
    }
    captured_patch_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "GET":
            return httpx.Response(200, json=get_response)
        captured_patch_body.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "evt3"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    await tools_calendar.calendar_respond_to_invite(
        {"mailbox": "a@company.com", "eventId": "evt3", "response": "accepted"}
    )
    patched_attendees = {a["email"]: a["responseStatus"] for a in captured_patch_body["attendees"]}
    assert patched_attendees == {
        "a@company.com": "accepted",
        "bob@customer.com": "needsAction",
        "carol@customer.com": "accepted",
    }


async def test_calendar_update_event_adds_an_attendee_without_uninviting_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same defect `calendar_respond_to_invite` was fixed for, on the tool an
    agent actually reaches for when asked to "add Jane to the meeting":
    events.patch REPLACES `attendees` wholesale. PATCHing only the address the
    caller named removes everyone else from the meeting -- HTTP 200, no error,
    external attendees quietly uninvited. So an update that names `attendees`
    GETs the event first and PATCHes back the COMPLETE list.

    The existing entries must come back UNTOUCHED, `responseStatus` included:
    re-stating an attendee as a bare `{"email": ...}` would reset an answer the
    person already gave."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import calendar as tools_calendar

    get_response = {
        "id": "evt7",
        "attendees": [
            {"email": "a@company.com", "responseStatus": "accepted", "organizer": True},
            {"email": "bob@customer.com", "responseStatus": "declined"},
            {"email": "carol@customer.com", "responseStatus": "needsAction"},
        ],
    }
    methods: list[str] = []
    captured_patch_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=get_response)
        captured_patch_body.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "evt7"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    await tools_calendar.calendar_update_event(
        {
            "mailbox": "a@company.com",
            "eventId": "evt7",
            "summary": "Kickoff",
            "start": "2026-08-20T10:00:00Z",
            "end": "2026-08-20T11:00:00Z",
            # Only the newcomer, which is what a model asked to "add Jane" sends.
            "attendees": ["jane@partner.com"],
        }
    )
    assert methods == ["GET", "PATCH"]
    attendees = captured_patch_body["attendees"]
    assert [a["email"] for a in attendees] == [
        "a@company.com",
        "bob@customer.com",
        "carol@customer.com",
        "jane@partner.com",
    ]
    assert attendees[0] == {
        "email": "a@company.com",
        "responseStatus": "accepted",
        "organizer": True,
    }
    assert attendees[1]["responseStatus"] == "declined"


async def test_calendar_update_event_does_not_re_add_an_attendee_already_on_the_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that helpfully passes back the whole list it just read must not
    end up with the same person twice -- and matching is case-insensitive,
    because email local-parts are in practice and Google treats them so."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import calendar as tools_calendar

    captured_patch_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "evt8",
                    "attendees": [{"email": "Bob@Customer.com", "responseStatus": "accepted"}],
                },
            )
        captured_patch_body.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "evt8"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    await tools_calendar.calendar_update_event(
        {
            "mailbox": "a@company.com",
            "eventId": "evt8",
            "summary": "Kickoff",
            "start": "2026-08-20T10:00:00Z",
            "end": "2026-08-20T11:00:00Z",
            "attendees": ["bob@customer.com", "jane@partner.com"],
        }
    )
    assert [a["email"] for a in captured_patch_body["attendees"]] == [
        "Bob@Customer.com",
        "jane@partner.com",
    ]


async def test_a_reschedule_is_still_a_single_patch_with_no_extra_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read-modify-write is paid for ONLY when `attendees` is part of the
    change. An update that cannot possibly drop an attendee -- a new time, a new
    title -- must stay the one call it always was; a GET before every reschedule
    would be pure waste."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import calendar as tools_calendar

    methods: list[str] = []
    captured_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        methods.append(request.method)
        captured_body.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "evt9"})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    await tools_calendar.calendar_update_event(
        {
            "mailbox": "a@company.com",
            "eventId": "evt9",
            "summary": "Kickoff (moved)",
            "start": "2026-08-21T14:00:00Z",
            "end": "2026-08-21T15:00:00Z",
        }
    )
    assert methods == ["PATCH"]
    assert "attendees" not in captured_body
    assert captured_body["start"] == {"dateTime": "2026-08-21T14:00:00Z"}


async def test_calendar_respond_to_invite_errors_clearly_if_the_mailbox_is_not_an_attendee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_bridge import google_api
    from mcp_bridge.tools import calendar as tools_calendar

    monkeypatch.setenv("GOOGLE_DELEGATED_MAILBOXES", "a@company.com")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "evt3", "attendees": [{"email": "someone-else@company.com"}]}
        )

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    with pytest.raises(RuntimeError, match="not an attendee"):
        await tools_calendar.calendar_respond_to_invite(
            {"mailbox": "a@company.com", "eventId": "evt3", "response": "accepted"}
        )


async def test_calendar_check_free_busy_sends_the_real_address_as_the_item_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whole-branch review I2(a). `FreeBusyRequestItem.id` is a CALENDAR ID.
    `primary` is a PATH-segment alias (`/calendars/primary/...`) and is not
    resolved there -- and `freeBusy` answers an unknown calendar with HTTP
    **200** carrying a per-calendar `errors` block, so `_unwrap` raises nothing
    and the model reads a failure as an answer. The old test asserted only the
    request path and so covered neither this nor I2(b) below.

    The default `mailbox` (omitted here) must resolve through
    `resolve_mailbox_address` to the connection's configured default -- the
    real address -- not to the literal string "primary"."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import calendar as tools_calendar

    monkeypatch.setenv("GOOGLE_DELEGATED_MAILBOXES", "assistant@company.com")
    monkeypatch.setenv("GOOGLE_DEFAULT_MAILBOX", "assistant@company.com")

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"calendars": {}})

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    await tools_calendar.calendar_check_free_busy(
        {"timeMin": "2026-08-20T00:00:00Z", "timeMax": "2026-08-21T00:00:00Z"}
    )
    assert captured["path"] == "/calendar/v3/freeBusy"
    body = captured["body"]
    assert body["items"] == [{"id": "assistant@company.com"}]
    assert body["timeMin"] == "2026-08-20T00:00:00Z"
    assert body["timeMax"] == "2026-08-21T00:00:00Z"


async def test_calendar_check_free_busy_can_query_other_peoples_calendars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whole-branch review I2(b). Free/busy exists to find a slot across
    COLLEAGUES' calendars; a tool that can only ask about the mailbox it
    authenticates as is close to useless for the one job it has. `items` takes
    a list, so `calendars` is passed straight through alongside the acting
    mailbox -- de-duplicated, and with the acting mailbox first."""
    from mcp_bridge import google_api
    from mcp_bridge.tools import calendar as tools_calendar

    monkeypatch.setenv("GOOGLE_DELEGATED_MAILBOXES", "a@company.com")

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "calendars": {
                    "a@company.com": {"busy": []},
                    "bob@company.com": {"busy": []},
                    "carol@partner.com": {"busy": []},
                }
            },
        )

    monkeypatch.setattr(
        google_api, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(google_api, "resolve_mailbox_token", lambda mailbox: "tok")

    await tools_calendar.calendar_check_free_busy(
        {
            "mailbox": "a@company.com",
            "timeMin": "2026-08-20T00:00:00Z",
            "timeMax": "2026-08-21T00:00:00Z",
            # The acting mailbox repeated on purpose: it must not be asked twice.
            "calendars": ["bob@company.com", "carol@partner.com", "a@company.com"],
        }
    )
    assert captured["body"]["items"] == [
        {"id": "a@company.com"},
        {"id": "bob@company.com"},
        {"id": "carol@partner.com"},
    ]
