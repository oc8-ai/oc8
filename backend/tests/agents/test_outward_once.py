"""A person is reached once per task, whatever the model does.

Live, 2026-07-28: one helpdesk ticket, one customer question, two complete
answers written into the chatter inside a single run. Both calls were
individually legitimate -- different bodies, so idempotency saw two different
calls -- and the policy engine only ever asked "may this agent send?", which is
yes both times. The customer got two answers to one question.

The mission was told "exactly one customer-visible message per run" and the
model ignored it. That is the whole reason this lives in the core: a rule the
model may decline to follow is not a guardrail.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.outward import (
    already_delivered,
    outward_target,
    remember_delivery,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

FOCUS_SPEC: dict[str, Any] = {
    "entity_field": "model",
    "id_fields": ["id", "record_id"],
    "labels": {"helpdesk.ticket": "Ticket"},
}
OUTWARD = ["post_message"]


async def _task(db: Any, tenant: uuid.UUID) -> m.Task:
    dept = m.Department(tenant_id=tenant, name="Kundenservice", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant, department_id=dept.id, name="Sina", status="running",
        narrowing={}, definition={}, presentation={},
    )
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant, department_id=dept.id, assigned_agent_id=agent.id,
        title="Bearbeite ein Ticket", state="in_progress",
    )
    db.add(task)
    await db.flush()
    return task


# ------------------------------------------------------------------ the target

def test_a_tool_that_reaches_nobody_has_no_target() -> None:
    """The overwhelmingly common case, and it must cost nothing."""
    assert outward_target("update_record", {"model": "helpdesk.ticket", "record_id": 73},
                          FOCUS_SPEC, OUTWARD) is None


def test_a_connection_that_declares_nothing_is_unguarded() -> None:
    """An empty declaration leaves behaviour exactly as it was -- a connection
    that has not thought about this must not silently gain a limit."""
    assert outward_target("post_message", {"model": "helpdesk.ticket", "record_id": 73},
                          FOCUS_SPEC, None) is None


def test_the_target_is_the_recipient_not_the_message() -> None:
    """Two different bodies to the same ticket are ONE recipient. Keying on the
    arguments -- which is what idempotency does -- is exactly what let the
    duplicate through."""
    first = outward_target(
        "post_message",
        {"model": "helpdesk.ticket", "record_id": 73, "body": "Guten Tag ..."},
        FOCUS_SPEC, OUTWARD,
    )
    second = outward_target(
        "post_message",
        {"model": "helpdesk.ticket", "record_id": 73, "body": "Nachtrag: ..."},
        FOCUS_SPEC, OUTWARD,
    )
    assert first == second == "helpdesk.ticket#73"


def test_two_records_are_two_recipients() -> None:
    a = outward_target("post_message", {"model": "helpdesk.ticket", "record_id": 73},
                       FOCUS_SPEC, OUTWARD)
    b = outward_target("post_message", {"model": "helpdesk.ticket", "record_id": 74},
                       FOCUS_SPEC, OUTWARD)
    assert a != b


def test_the_entity_may_be_named_by_the_tool_instead_of_an_argument() -> None:
    """Both shapes `focus_spec` declares, not just `entity_field`. Software with
    one endpoint per KIND of thing (`create_event`, `create_issue`) says which
    kind in the TOOL NAME and carries no argument repeating it -- reading only
    the argument left every such call sharing one target, i.e. one create per
    task with no second call expressible. `tool_entities` is the same seam
    `describe_focus` and `record_identity` already resolve through.
    """
    spec: dict[str, Any] = {
        "tool_entities": {"create_event": "event"},
        "id_fields": ["start"],
    }
    first = outward_target("create_event", {"start": "10:00"}, spec, ["create_event"])
    second = outward_target("create_event", {"start": "11:00"}, spec, ["create_event"])
    assert first == "event#10:00"
    assert first != second


def test_a_call_that_names_no_record_still_has_a_target() -> None:
    """Coarser on purpose: a guard that gives up when it cannot identify the
    recipient guards nothing, and a run has no business broadcasting twice."""
    assert outward_target("post_message", {}, FOCUS_SPEC, OUTWARD) is not None


def test_two_different_calls_that_name_no_record_are_two_targets() -> None:
    """E2E N1: every new message of a chat shares one task, and keying a call
    without a record on the tool alone allowed one new message per chat,
    ever. Without a declared recipient, the call itself is the target."""
    first = outward_target("post_message", {"to": "a", "body": "x"}, FOCUS_SPEC, OUTWARD)
    second = outward_target("post_message", {"to": "b", "body": "y"}, FOCUS_SPEC, OUTWARD)
    assert first is not None and second is not None
    assert first != second


def test_the_same_call_that_names_no_record_is_one_target() -> None:
    args = {"to": "a", "body": "x"}
    assert outward_target("post_message", dict(args), FOCUS_SPEC, OUTWARD) == outward_target(
        "post_message", dict(args), FOCUS_SPEC, OUTWARD
    )


def test_declared_target_fields_identify_who_a_call_without_a_record_reaches() -> None:
    """A connection may declare which arguments say who a call reaches
    (`focus_spec.outward_fields`): a reworded body to the same recipient is
    the duplicate the guard exists for, another recipient is not."""
    spec = {**FOCUS_SPEC, "outward_fields": {"post_message": ["to", "subject"]}}
    first = outward_target("post_message", {"to": "a", "subject": "S", "body": "x"}, spec, OUTWARD)
    reworded = outward_target(
        "post_message", {"to": "a", "subject": "S", "body": "y"}, spec, OUTWARD
    )
    other = outward_target("post_message", {"to": "b", "subject": "S", "body": "x"}, spec, OUTWARD)
    assert first == reworded
    assert first != other


# ------------------------------------------------------------------ the memory

async def test_the_second_message_to_one_record_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        task = await _task(db, tenant)
        target = "helpdesk.ticket#73"
        assert not await already_delivered(
            db, tenant_id=tenant, task_id=task.id, target=target
        )
        await remember_delivery(db, tenant_id=tenant, task_id=task.id, target=target)
        assert await already_delivered(
            db, tenant_id=tenant, task_id=task.id, target=target
        )


async def test_another_record_is_unaffected(app_session: AppSessionFactory) -> None:
    """One answer per ticket, not one answer per run: a run that legitimately
    works two records must be able to answer both."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        task = await _task(db, tenant)
        await remember_delivery(
            db, tenant_id=tenant, task_id=task.id, target="helpdesk.ticket#73"
        )
        assert not await already_delivered(
            db, tenant_id=tenant, task_id=task.id, target="helpdesk.ticket#74"
        )


async def test_another_task_is_unaffected(app_session: AppSessionFactory) -> None:
    """The next run may answer the same ticket again -- by then the customer has
    replied, and refusing that would be the dead end this all exists to avoid."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        first = await _task(db, tenant)
        second = await _task(db, tenant)
        await remember_delivery(
            db, tenant_id=tenant, task_id=first.id, target="helpdesk.ticket#73"
        )
        assert not await already_delivered(
            db, tenant_id=tenant, task_id=second.id, target="helpdesk.ticket#73"
        )


async def test_remembering_twice_does_not_raise(app_session: AppSessionFactory) -> None:
    """Two containers can attempt the same call after a restart. The loser must
    not fail the run with a unique violation."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        task = await _task(db, tenant)
        await remember_delivery(
            db, tenant_id=tenant, task_id=task.id, target="helpdesk.ticket#73"
        )
        await remember_delivery(
            db, tenant_id=tenant, task_id=task.id, target="helpdesk.ticket#73"
        )
        assert await already_delivered(
            db, tenant_id=tenant, task_id=task.id, target="helpdesk.ticket#73"
        )
