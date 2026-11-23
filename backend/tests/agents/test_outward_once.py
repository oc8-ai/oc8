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


def test_a_call_without_declared_recipients_stays_once_per_task() -> None:
    """Fail closed: nothing says who an undeclared call reaches, so any two of
    them may reach the same person -- one per task, whatever the arguments."""
    first = outward_target("post_message", {"to": "a", "body": "x"}, FOCUS_SPEC, OUTWARD)
    second = outward_target("post_message", {"to": "b", "body": "y"}, FOCUS_SPEC, OUTWARD)
    assert first == second == "post_message#"


SEND_SPEC: dict[str, Any] = {
    **FOCUS_SPEC,
    "outward_fields": {"post_message": ["to", "cc", "bcc"], "forward": ["to"]},
}
SENDS = ["post_message", "forward"]


def _send(**arguments: Any) -> str | None:
    return outward_target("post_message", arguments, SEND_SPEC, SENDS)


def test_declared_recipients_are_the_target_whatever_the_subject() -> None:
    """E2E N1 under the security ruling: the key is who a call reaches, so a
    reworded subject to the same person is the repeat the guard exists for,
    and another person is another target."""
    first = _send(to=["bob@example.com"], subject="Offer", body="x")
    assert _send(to=["bob@example.com"], subject="Offer, again", body="y") == first
    assert _send(to=["ann@example.com"], subject="Offer", body="x") != first


def test_the_recipient_key_ignores_case_order_duplicates_and_the_field() -> None:
    first = _send(to=["Bob@Example.com ", "ann@example.com"], cc=[])
    assert _send(to=["ann@example.com"], cc=["bob@example.com", "BOB@example.com"]) == first
    assert _send(to="Ann <ann@example.com>, bob@example.com") == first


def test_declared_tools_share_one_recipient_key() -> None:
    """Forwarding to the person a message was already sent to reaches them a
    second time just the same."""
    assert outward_target(
        "forward", {"to": ["bob@example.com"]}, SEND_SPEC, SENDS
    ) == _send(to=["bob@example.com"])


def test_a_declared_call_naming_nobody_falls_back_to_once_per_task() -> None:
    assert _send(subject="no recipients") == "post_message#"


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


# ------------------------------------------------- recipients, one by one (review)


def test_a_display_name_with_a_comma_is_one_person() -> None:
    """`"Doe, Ann" <ann@x.com>` is Ann, not "Doe" plus Ann: a split on the
    comma made it a different key from `ann@x.com`, so a second mail to Ann
    passed the guard."""
    assert _send(to='"Doe, Ann" <ann@x.com>') == _send(to=["ann@x.com"])
    assert _send(to=['"Doe, Ann" <ANN@X.COM>']) == _send(to=["ann@x.com"])


def test_only_valid_addresses_count() -> None:
    assert _send(to=["not an address", "ann@x.com", ""]) == _send(to=["ann@x.com"])
    assert _send(to=["nobody"]) == "post_message#"


def test_plus_addressing_is_another_mailbox() -> None:
    assert _send(to=["ann+billing@x.com"]) != _send(to=["ann@x.com"])


def test_an_international_domain_is_one_domain() -> None:
    assert _send(to=["ann@bücher.de"]) == _send(to=["ann@xn--bcher-kva.de"])


async def _reached(db: Any, tenant: uuid.UUID, task: m.Task, **arguments: Any) -> bool:
    target = _send(**arguments)
    assert target is not None
    return await already_delivered(db, tenant_id=tenant, task_id=task.id, target=target)


async def _deliver(db: Any, tenant: uuid.UUID, task: m.Task, **arguments: Any) -> None:
    target = _send(**arguments)
    assert target is not None
    await remember_delivery(db, tenant_id=tenant, task_id=task.id, target=target)


async def test_a_send_reaching_anybody_already_reached_is_a_repeat(
    app_session: AppSessionFactory,
) -> None:
    """One marker per person: `[a]` then `[a, b]` reaches a a second time,
    and so does `to: a` then `cc: a` -- whatever else the second send names."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        task = await _task(db, tenant)
        await _deliver(db, tenant, task, to=["ann@x.com"])
        assert await _reached(db, tenant, task, to=["ann@x.com", "bob@x.com"])
        assert await _reached(db, tenant, task, to=["carl@x.com"], cc=["ANN@x.com"])
        assert await _reached(db, tenant, task, to='"Doe, Ann" <Ann@X.com>')
        assert not await _reached(db, tenant, task, to=["bob@x.com"])
        assert not await _reached(db, tenant, task, to=["ann+other@x.com"])
