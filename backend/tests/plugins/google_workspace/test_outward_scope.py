"""The outward-once cap must land on the EVENT, not on the tool name.

Three calendar tools joined `outward_tools` because an event with attendees
lands on those attendees' calendars, including at other companies. With nothing
for `outward_target` to key on, that classification degrades to "once per task,
period" -- and `events.insert` creates exactly one event per request and has no
batch form, so an agent asked to book three interview slots gets a refusal on
call 2 with nothing it can do differently. This asserts the manifest's
`focus_spec` really does scope the cap, using the REAL manifest and the REAL
core function rather than a hand-written spec that could drift from either.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.outward import already_delivered, outward_target, remember_delivery
from oc8.capas.discovery import find_plugin
from oc8.capas.manifest import parse_manifest
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_PLUGINS_DIR = Path(__file__).resolve().parents[4] / "capas"


def _connection_config() -> dict[str, Any]:
    """The ASSEMBLED manifest's connection config.

    Goes through `find_plugin`, not `tomllib` on `plugin.toml`: the tool pack
    now lives in a sibling `tool_pack.toml` (design §2/§3), so reading
    `plugin.toml` alone would see only the slim core and this whole file would
    be asserting against a manifest that has no connections at all.
    """
    discovered = find_plugin("google_workspace", [str(_PLUGINS_DIR)])
    assert discovered is not None, "google_workspace was not discovered at all"
    assert discovered.valid, f"google_workspace manifest is invalid: {discovered.error}"
    assert discovered.manifest is not None
    manifest = parse_manifest(discovered.manifest)
    assert manifest.tool_pack is not None
    return dict(manifest.tool_pack.connections[0].config)


def _target(tool: str, arguments: dict[str, Any]) -> str | None:
    config = _connection_config()
    return outward_target(tool, arguments, config.get("focus_spec"), config.get("outward_tools"))


def _create(summary: str, start: str) -> dict[str, Any]:
    return {
        "mailbox": "assistant@company.com",
        "summary": summary,
        "start": start,
        "end": start,
    }


# --------------------------------------------------------------- the targeting


def test_two_distinct_meetings_are_two_recipients() -> None:
    first = _target("calendar_create_event", _create("Kickoff", "2026-08-20T10:00:00Z"))
    second = _target("calendar_create_event", _create("Follow-up", "2026-08-27T10:00:00Z"))
    assert first is not None and second is not None
    assert first != second


def test_three_interview_slots_are_three_recipients() -> None:
    """The case the manifest keys on `start` for: three bookings that share one
    title and differ only in when they are."""
    targets = {
        _target("calendar_create_event", _create("Interview", when))
        for when in (
            "2026-08-20T10:00:00Z",
            "2026-08-20T11:00:00Z",
            "2026-08-20T12:00:00Z",
        )
    }
    assert len(targets) == 3


def test_the_same_event_updated_twice_is_one_recipient() -> None:
    """The cap is scoped, not removed. Two updates to one event are two rounds
    of "the meeting changed" landing on the same people, and the second is
    refused -- however the summary is reworded in between, because an update
    keys on the event id."""
    first = _target(
        "calendar_update_event",
        {
            "eventId": "evt1",
            "summary": "Kickoff",
            "start": "2026-08-20T10:00:00Z",
            "end": "2026-08-20T11:00:00Z",
        },
    )
    second = _target(
        "calendar_update_event",
        {
            "eventId": "evt1",
            "summary": "Kickoff (moved)",
            "start": "2026-08-21T14:00:00Z",
            "end": "2026-08-21T15:00:00Z",
        },
    )
    assert first == second == "calendar_event#evt1"


def test_two_different_events_updated_are_two_recipients() -> None:
    a = _target(
        "calendar_update_event",
        {"eventId": "evt1", "summary": "A", "start": "x", "end": "y"},
    )
    b = _target(
        "calendar_update_event",
        {"eventId": "evt2", "summary": "A", "start": "x", "end": "y"},
    )
    assert a != b


def test_a_meet_link_at_the_same_moment_is_the_same_recipient() -> None:
    """Deliberate: a plain event and a Meet-linked one at the same moment are
    two invitations to the same people for the same slot. All three creating
    tools share the `calendar_event` entity so that duplicate is caught."""
    plain = _target("calendar_create_event", _create("Kickoff", "2026-08-20T10:00:00Z"))
    meet = _target("calendar_create_meet_link", _create("Kickoff", "2026-08-20T10:00:00Z"))
    assert plain == meet


def test_the_mail_tools_are_unchanged_by_the_calendar_scoping() -> None:
    """`focus_spec` is per CONNECTION, so it is seen by every tool. Gmail names
    no event: a send is keyed by its declared recipients (E2E N1), and the
    calendar seam changes neither that nor the invite reply's per-task cap."""
    mail = {"to": ["bob@customer.com"], "subject": "Hi", "body": "..."}
    first = _target("gmail_send", mail)
    assert first is not None
    assert _target("gmail_send", {**mail, "subject": "Other"}) == first
    assert _target("gmail_reply", {"messageId": "m1", "to": ["BOB@customer.com"]}) == first
    assert _target("gmail_send", {**mail, "to": ["ann@customer.com"]}) != first
    assert (
        _target("calendar_respond_to_invite", {"eventId": "evt1", "response": "accepted"})
        == "calendar_respond_to_invite#"
    )


def test_a_tool_that_reaches_nobody_still_has_no_target() -> None:
    assert _target("calendar_list_events", {"timeMin": "a", "timeMax": "b"}) is None
    assert _target("calendar_get_event", {"eventId": "evt1"}) is None


# ------------------------------------------------ the cap, against the real store


async def _task(db: Any, tenant: uuid.UUID) -> m.Task:
    dept = m.Department(tenant_id=tenant, name="Office", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
        status="running",
        narrowing={},
        definition={},
        presentation={},
    )
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=agent.id,
        title="Plane die Gespräche",
        state="in_progress",
    )
    db.add(task)
    await db.flush()
    return task


async def test_a_task_can_create_two_events_but_not_update_one_twice(
    app_session: AppSessionFactory,
) -> None:
    """The whole finding, end to end through the same two functions the gateway
    calls: two distinct meetings both go out, and the second change to one
    meeting does not."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        task = await _task(db, tenant)

        first = _target("calendar_create_event", _create("Kickoff", "2026-08-20T10:00:00Z"))
        second = _target("calendar_create_event", _create("Retro", "2026-08-24T16:00:00Z"))
        assert first is not None and second is not None

        assert not await already_delivered(db, tenant_id=tenant, task_id=task.id, target=first)
        await remember_delivery(db, tenant_id=tenant, task_id=task.id, target=first)
        # The second event is a different recipient: it still goes out.
        assert not await already_delivered(db, tenant_id=tenant, task_id=task.id, target=second)
        await remember_delivery(db, tenant_id=tenant, task_id=task.id, target=second)

        update = _target(
            "calendar_update_event",
            {"eventId": "evt1", "summary": "Kickoff", "start": "x", "end": "y"},
        )
        assert update is not None
        await remember_delivery(db, tenant_id=tenant, task_id=task.id, target=update)
        # ... and the SAME event, changed again, is refused.
        assert await already_delivered(db, tenant_id=tenant, task_id=task.id, target=update)
