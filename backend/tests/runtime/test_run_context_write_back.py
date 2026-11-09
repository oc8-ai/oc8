"""A request's context write-back sends only what the request changed, so a
key merged meanwhile by a tool (or anyone else) survives it."""

from __future__ import annotations

import copy
import uuid

import pytest

from oc8 import models as m
from oc8.runtime.run_context import merge_context, write_back_context
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_write_back_keeps_concurrent_keys_and_applies_its_own(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            state="running",
            context={"steps": 1, "transcript": [{"a": 1}], "legacy": True, "shared": "old"},
        )
        db.add(run)
        await db.flush()

        ctx = dict(run.context)
        before = copy.deepcopy(ctx)
        # A tool merges during the request ...
        await merge_context(db, run, {"suggestions": ["Yes"], "shared": "tool"})
        # ... while the request edits its own copy, in place too.
        ctx["steps"] = 2
        ctx["transcript"].append({"b": 2})
        ctx.pop("legacy")
        ctx["shared"] = "request"
        stored = await write_back_context(db, run, before=before, after=ctx)
        await db.refresh(run)

    assert stored == run.context
    assert run.context == {
        "steps": 2,
        "transcript": [{"a": 1}, {"b": 2}],
        "suggestions": ["Yes"],
        # Both changed it: the request writes last, as before.
        "shared": "request",
    }


async def test_an_unchanged_context_writes_nothing(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running", context={"x": 1})
        db.add(run)
        await db.flush()
        before = copy.deepcopy(run.context)
        await merge_context(db, run, {"y": 2})
        await write_back_context(db, run, before=before, after=dict(before))
        await db.refresh(run)
    assert run.context == {"x": 1, "y": 2}


async def test_concurrent_write_backs_keep_each_others_keys(
    app_session: AppSessionFactory,
) -> None:
    """Two /tool requests of one run, from the same starting snapshot, each
    writing its own key: one statement each, so neither erases the other."""
    import asyncio

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running", context={"x": 0})
        db.add(run)
        await db.flush()
        run_id = run.id

    started = asyncio.Event()

    async def request(key: str) -> None:
        async with app_session(tenant) as db:
            row = await db.get(m.AgentRun, run_id)
            assert row is not None
            before = copy.deepcopy(row.context)
            if key == "a":
                await write_back_context(db, row, before=before, after={**before, "a": 1})
                started.set()
                await asyncio.sleep(0.2)  # hold the row until the other one waits
            else:
                await started.wait()
                await write_back_context(db, row, before=before, after={**before, "b": 2})

    await asyncio.gather(request("a"), request("b"))
    async with app_session(tenant) as db:
        row = await db.get(m.AgentRun, run_id)
        assert row is not None
        assert row.context == {"x": 0, "a": 1, "b": 2}
