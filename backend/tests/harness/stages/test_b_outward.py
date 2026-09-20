"""B8: one message per recipient per task, checked BEFORE the call. The stage
wraps agent/outward.py's three functions with the exact guard conditions the
two runtimes used inline; outward.py itself is covered by
tests/agents/test_outward_once.py."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.harness.stages.b_outward import check_outward, remember_outward
from oc8.agent.outward import REFUSAL
from oc8.modelrouter import ToolCall
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

# Real shapes, per tests/agents/test_outward_once.py: `outward_target` resolves
# the entity from `tool_entities` (the tool name IS the entity, no argument
# repeats it) and the record id from `id_fields`.
_FOCUS: dict[str, Any] | None = {
    "tool_entities": {"send_reply": "ticket"},
    "id_fields": ["ticket_id"],
}
_OUTWARD: list[str] | None = ["send_reply"]


async def _task(db: object, tenant: uuid.UUID) -> uuid.UUID:
    dept = m.Department(tenant_id=tenant, name="Support", frame={})
    db.add(dept)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    task = m.Task(tenant_id=tenant, department_id=dept.id, title="t", state="in_progress")
    db.add(task)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    return task.id


async def test_a_tool_that_reaches_nobody_has_no_target_and_no_refusal(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        task_id = await _task(db, tenant)
        tc = ToolCall(id="1", name="search_records", arguments={"q": "x"})
        check = await check_outward(
            db, tenant_id=tenant, task_id=task_id, tc=tc, focus_spec=_FOCUS, outward_tools=_OUTWARD
        )
    assert check.target is None and check.refusal is None


async def test_the_second_message_to_one_recipient_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        task_id = await _task(db, tenant)
        tc = ToolCall(id="1", name="send_reply", arguments={"ticket_id": 7, "body": "hi"})
        first = await check_outward(
            db, tenant_id=tenant, task_id=task_id, tc=tc, focus_spec=_FOCUS, outward_tools=_OUTWARD
        )
        assert first.target is not None and first.refusal is None
        await remember_outward(
            db, tenant_id=tenant, task_id=task_id, target=first.target, output="sent"
        )
        second = await check_outward(
            db, tenant_id=tenant, task_id=task_id, tc=tc, focus_spec=_FOCUS, outward_tools=_OUTWARD
        )
    assert second.refusal == REFUSAL.format(target=first.target)


async def test_a_failed_send_is_not_remembered(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        task_id = await _task(db, tenant)
        tc = ToolCall(id="1", name="send_reply", arguments={"ticket_id": 7})
        first = await check_outward(
            db, tenant_id=tenant, task_id=task_id, tc=tc, focus_spec=_FOCUS, outward_tools=_OUTWARD
        )
        await remember_outward(
            db, tenant_id=tenant, task_id=task_id, target=first.target, output="ERROR: boom"
        )
        again = await check_outward(
            db, tenant_id=tenant, task_id=task_id, tc=tc, focus_spec=_FOCUS, outward_tools=_OUTWARD
        )
    assert again.refusal is None


async def test_without_a_task_nothing_is_checked_or_remembered(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        tc = ToolCall(id="1", name="send_reply", arguments={"ticket_id": 7})
        check = await check_outward(
            db, tenant_id=tenant, task_id=None, tc=tc, focus_spec=_FOCUS, outward_tools=_OUTWARD
        )
        assert check.target is not None and check.refusal is None
        await remember_outward(db, tenant_id=tenant, task_id=None, target=check.target, output="ok")
