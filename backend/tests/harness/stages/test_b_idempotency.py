"""B9: a write replayed within the same task returns the first result; reads
are never replayed; failures are never recorded. Package 1 wires this stage
into the isolated runtime only (spec §1.1) -- the in-process engine has no
replay protection today and gets it in package 5."""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.agent.harness.stages.b_idempotency import record_for, replay_for
from oc8.modelrouter import ToolCall
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _task(db: object, tenant: uuid.UUID) -> uuid.UUID:
    dept = m.Department(tenant_id=tenant, name="Sales", frame={})
    db.add(dept)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    task = m.Task(tenant_id=tenant, department_id=dept.id, title="t", state="in_progress")
    db.add(task)  # type: ignore[attr-defined]
    await db.flush()  # type: ignore[attr-defined]
    return task.id


async def test_a_recorded_write_is_replayed(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    tc = ToolCall(id="1", name="create_record", arguments={"model": "x", "values": {"a": 1}})
    async with app_session(tenant) as db:
        task_id = await _task(db, tenant)
        assert await replay_for(db, tenant_id=tenant, task_id=task_id, tc=tc, writes=True) is None
        await record_for(
            db, tenant_id=tenant, task_id=task_id, tc=tc, writes=True, output="created id=1"
        )
        replay = await replay_for(db, tenant_id=tenant, task_id=task_id, tc=tc, writes=True)
    assert replay is not None
    assert "created id=1" in replay
    assert "replay" in replay.lower()


async def test_reads_are_never_replayed(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    tc = ToolCall(id="1", name="search_records", arguments={"model": "x"})
    async with app_session(tenant) as db:
        task_id = await _task(db, tenant)
        await record_for(db, tenant_id=tenant, task_id=task_id, tc=tc, writes=False, output="r")
        assert await replay_for(db, tenant_id=tenant, task_id=task_id, tc=tc, writes=False) is None


async def test_a_failure_is_not_recorded(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    tc = ToolCall(id="1", name="create_record", arguments={"model": "x"})
    async with app_session(tenant) as db:
        task_id = await _task(db, tenant)
        await record_for(
            db, tenant_id=tenant, task_id=task_id, tc=tc, writes=True, output="ERROR: boom"
        )
        assert await replay_for(db, tenant_id=tenant, task_id=task_id, tc=tc, writes=True) is None


async def test_without_a_task_nothing_happens(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    tc = ToolCall(id="1", name="create_record", arguments={"model": "x"})
    async with app_session(tenant) as db:
        await record_for(db, tenant_id=tenant, task_id=None, tc=tc, writes=True, output="ok")
        assert await replay_for(db, tenant_id=tenant, task_id=None, tc=tc, writes=True) is None
