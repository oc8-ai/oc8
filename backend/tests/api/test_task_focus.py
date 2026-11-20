"""What a task card says while an agent is working on it.

A scheduled agent's card carried the trigger's text and nothing else -- "check
the ticket inbox and work the oldest open ticket", on every card, every run,
forever. Which ticket it was actually on was visible only in the live log, and
only to whoever had that screen open at the time.

The tool gateway already derives a human phrase for each call from the
connection's own focus_spec ("Bearbeitet Ticket 43") for exactly that log. The
same phrase belongs on the task, where it survives the run and answers "what is
it doing" from the board.
"""

from __future__ import annotations

import json
import uuid

import pytest

from oc8 import models as m
from oc8.agent.tool_semantics import record_title
from oc8.realtime.emit import note_focus
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _task(db: object, tenant: uuid.UUID) -> m.Task:
    task = m.Task(
        tenant_id=tenant,
        department_id=uuid.uuid4(),
        title="Prüfe den Ticket-Eingang",
        state="in_progress",
    )
    db.add(task)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    return task


async def test_the_focus_lands_on_the_task_and_in_the_feed(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with app_session(tenant) as db:
        task = await _task(db, tenant)
        await note_focus(
            db, tenant_id=tenant, agent_id=agent_id, task_id=task.id, focus="Bearbeitet Ticket 43"
        )
        await db.refresh(task)
        assert task.meta_label == "Bearbeitet Ticket 43"

        events = (await db.execute(m.ActivityEvent.__table__.select())).fetchall()
        assert any(e.message == "Bearbeitet Ticket 43" for e in events), "feed still gets it"


async def test_the_latest_focus_replaces_the_previous_one(
    app_session: AppSessionFactory,
) -> None:
    """The card answers "what NOW", not "what first" -- a run touches several
    records and only the current one is interesting."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        task = await _task(db, tenant)
        for focus in ("Öffnet Ticket 43", "Notiert an Ticket 43", "Bearbeitet Ticket 43"):
            await note_focus(
                db, tenant_id=tenant, agent_id=uuid.uuid4(), task_id=task.id, focus=focus
            )
        await db.refresh(task)
        assert task.meta_label == "Bearbeitet Ticket 43"


async def test_no_task_is_not_an_error(app_session: AppSessionFactory) -> None:
    """Not every tool call belongs to a task -- an ad-hoc chat turn has none, and
    the feed entry must still be written."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await note_focus(
            db, tenant_id=tenant, agent_id=uuid.uuid4(), task_id=None, focus="Durchsucht Tickets"
        )
        events = (await db.execute(m.ActivityEvent.__table__.select())).fetchall()
        assert any(e.message == "Durchsucht Tickets" for e in events)


async def test_a_task_that_has_gone_missing_does_not_kill_the_run(
    app_session: AppSessionFactory,
) -> None:
    """A label is cosmetic. Failing a tool call over one would trade a real
    action for a decoration."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await note_focus(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=uuid.uuid4(),  # never existed
            focus="Bearbeitet Ticket 99",
        )
        events = (await db.execute(m.ActivityEvent.__table__.select())).fetchall()
        assert any(e.message == "Bearbeitet Ticket 99" for e in events)


async def test_a_search_does_not_erase_which_record_is_being_worked(
    app_session: AppSessionFactory,
) -> None:
    """The card has to answer "which ticket", and a search names none.

    Live, 2026-07-28: 20 of 32 cards read "Durchsucht Ticket" -- identical on
    every one of them, and useless. Runs search several times (for the stages,
    for the queue) and any of those calls landing last wiped the ticket the run
    had actually worked.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        task = await _task(db, tenant)
        await note_focus(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=task.id,
            focus="Bearbeitet Ticket #43",
            specific=True,
        )
        await note_focus(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=task.id,
            focus="Durchsucht Ticket",
            specific=False,
        )
        await db.refresh(task)
        assert task.meta_label == "Bearbeitet Ticket #43"


async def test_a_later_record_still_replaces_an_earlier_one(
    app_session: AppSessionFactory,
) -> None:
    """Only the vague loses to the specific -- a run that moves on to another
    ticket must still say so."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        task = await _task(db, tenant)
        for focus in ("Öffnet Ticket #43", "Bearbeitet Ticket #44"):
            await note_focus(
                db,
                tenant_id=tenant,
                agent_id=uuid.uuid4(),
                task_id=task.id,
                focus=focus,
                specific=True,
            )
        await db.refresh(task)
        assert task.meta_label == "Bearbeitet Ticket #44"


async def test_a_search_still_labels_a_task_that_has_nothing_yet(
    app_session: AppSessionFactory,
) -> None:
    """A run that only ever searched -- because the queue was empty -- should say
    that rather than show a blank card."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        task = await _task(db, tenant)
        await note_focus(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=task.id,
            focus="Durchsucht Ticket",
            specific=False,
        )
        await db.refresh(task)
        assert task.meta_label == "Durchsucht Ticket"


async def test_a_record_url_lands_on_the_card_and_in_the_feed(
    app_session: AppSessionFactory,
) -> None:
    """The phrase says which record; the URL is how an operator opens it."""
    tenant = uuid.uuid4()
    url = "https://odoo.example/web#id=43&model=helpdesk.ticket&view_type=form"
    async with app_session(tenant) as db:
        task = await _task(db, tenant)
        await note_focus(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=task.id,
            focus="Bearbeitet Ticket 43",
            record_url=url,
        )
        await db.refresh(task)
        assert (task.payload or {}).get("record_url") == url
        events = (await db.execute(m.ActivityEvent.__table__.select())).fetchall()
        assert any(e.detail == url for e in events)


# --------------------------------------------------------------- record titles


def test_a_title_is_read_out_of_the_result_the_call_returned() -> None:
    """The subject is not in the ARGUMENTS -- update_record carries an id and the
    fields to change, nothing else. It is in the RESULT, which is why the card
    can name it at all without core learning to read an Odoo record.

    Which keys hold it is the plugin's business; core only walks the JSON.
    """
    spec = {"id_field": "id", "title_fields": ["display_name", "name"]}
    result = json.dumps(
        {
            "success": True,
            "record": {"id": 55, "display_name": "Rechnung 2026-0412 doppelt abgebucht"},
            "url": "http://odoo/helpdesk.ticket/55",
        }
    )
    assert record_title(result, "55", spec) == "Rechnung 2026-0412 doppelt abgebucht"


def test_the_title_of_a_different_record_is_not_borrowed() -> None:
    """A search returns many records. Taking the first one's name would label the
    card with a ticket the run never touched."""
    spec = {"id_field": "id", "title_fields": ["name"]}
    result = json.dumps(
        {"records": [{"id": 12, "name": "Fremdes Ticket"}, {"id": 55, "name": "Unseres"}]}
    )
    assert record_title(result, "55", spec) == "Unseres"
    assert record_title(result, "99", spec) is None


def test_a_result_that_is_not_json_is_simply_no_title() -> None:
    """Plenty of tools answer in prose, and a label is cosmetic -- it may never
    cost a call."""
    spec = {"id_field": "id", "title_fields": ["name"]}
    assert record_title("Successfully updated record 55", "55", spec) is None
    assert record_title("", "55", spec) is None
    assert record_title(json.dumps({"id": 55}), "55", {}) is None
