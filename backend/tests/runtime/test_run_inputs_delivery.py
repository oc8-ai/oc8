"""Steer or queue (spec §5.5): a `next_turn` input is never read by the
running run; it becomes the chat's next turn when the run ends -- DONE or
FAILED -- and is cancelled when the run is stopped. An approval wait is not an
end: the input waits through it."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.engine import RunResult
from oc8.runtime.repository import RunRepository
from oc8.runtime.run_inputs import (
    CANCELLED_STEP,
    CARRIED_OVER_STEP,
    ConsumedInput,
    add_run_input,
    consume_run_inputs,
    has_pending_run_inputs,
    take_run_inputs,
)
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory
from tests.runtime.test_carried_over_inputs import _execute, _state, _World, _world
from tests.runtime.test_executor import _FnRuntime

pytestmark = pytest.mark.asyncio


async def _write(
    app_session: AppSessionFactory, world: _World, text: str, *, delivery: str
) -> uuid.UUID:
    async with app_session(world.tenant) as w:
        run = await w.get(m.AgentRun, world.run_id)
        member = await w.get(m.OrgMember, world.member_id)
        assert run is not None and member is not None
        row = await add_run_input(w, run=run, member=member, text=text, delivery=delivery)  # type: ignore[arg-type]
        await w.commit()
        return row.id


def _ending(
    app_session: AppSessionFactory,
    world: _World,
    *,
    status: str,
    writes: tuple[tuple[str, str], ...] = (("Danach bitte die Liste.", "next_turn"),),
) -> _FnRuntime:
    """A run written to on its last step that ends in `status`."""

    async def fn(db: Any, **kw: Any) -> RunResult:
        for text, delivery in writes:
            await _write(app_session, world, text, delivery=delivery)
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status=status,
            output="Es sind 3 offen.",
            tool_calls=[],
            steps=1,
        )

    return _FnRuntime(fn)


async def test_next_turn_is_never_injected_into_the_running_run(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session, messenger=False)
    queued = await _write(app_session, world, "danach", delivery="next_turn")

    async with app_session(world.tenant) as db:
        assert not await has_pending_run_inputs(db, run_id=world.run_id)
        assert await consume_run_inputs(db, run_id=world.run_id, step=1) == []
        await db.commit()
        assert await take_run_inputs(world.tenant, run_id=world.run_id, step=1, probe=db) == []
    assert await take_run_inputs(world.tenant, run_id=world.run_id, step=1) == []

    steer = await _write(app_session, world, "jetzt", delivery="steer")
    async with app_session(world.tenant) as db:
        taken = await consume_run_inputs(db, run_id=world.run_id, step=2)
        await db.commit()
    assert [c.id for c in taken] == [steer], "only the steer input"
    _runs, _messages, inputs = await _state(app_session, world)
    waiting = next(i for i in inputs if i.id == queued)
    assert waiting.consumed_at is None and waiting.delivery == "next_turn"


async def test_next_turn_becomes_the_next_turn_when_the_run_ends(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    world = await _world(app_session, messenger=False)

    await _execute(world, _ending(app_session, world, status="done"))

    runs, messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["done", "queued"], "exactly one follow-up run"
    assert [i.consumed_step for i in inputs] == [CARRIED_OVER_STEP]
    assert [msg.role for msg in messages] == ["assistant", "user"]
    assert messages[-1].content == "Danach bitte die Liste."
    assert runs[1].context["reply_to_message_id"] == str(messages[-1].id)


async def test_next_turn_carries_over_after_a_failed_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    world = await _world(app_session, messenger=False)

    await _execute(
        world,
        _ending(
            app_session,
            world,
            status="failed",
            writes=(("zu spät gelesen", "steer"), ("Danach bitte die Liste.", "next_turn")),
        ),
    )

    runs, messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["failed", "queued"]
    steer, queued = inputs
    assert steer.consumed_at is None, "a failed run's steer input stays not read"
    assert queued.consumed_step == CARRIED_OVER_STEP
    assert messages[-1].role == "user" and messages[-1].content == "Danach bitte die Liste."


async def test_next_turn_is_cancelled_when_the_run_is_stopped(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[list[ConsumedInput]] = []

    async def _publish(
        tenant_id: uuid.UUID, *, run_id: uuid.UUID, consumed: list[ConsumedInput]
    ) -> None:
        published.append(list(consumed))

    monkeypatch.setattr("oc8.runtime.run_inputs.publish_inputs_consumed", _publish)
    world = await _world(app_session, messenger=False)

    await _execute(world, _ending(app_session, world, status="interrupted"))

    runs, _messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["interrupted"], "stop means stop"
    [queued] = inputs
    assert queued.consumed_at is not None and queued.consumed_step == CANCELLED_STEP
    assert [[c.id for c in batch] for batch in published] == [[queued.id]]
    assert published[0][0].step == CANCELLED_STEP


async def test_next_turn_survives_an_approval_wait(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    world = await _world(app_session, messenger=False)

    await _execute(world, _ending(app_session, world, status="waiting_for_approval"))

    runs, messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["waiting_for_approval"]
    assert [i.consumed_at for i in inputs] == [None], "an approval wait is not an end"
    assert messages == []

    # The card is decided: the run is queued again and finishes.
    async with app_session(world.tenant) as db:
        run = await db.get(m.AgentRun, world.run_id)
        assert run is not None
        await RunRepository(db).transition(run, RunState.QUEUED)
        await db.commit()
    await _execute(world, _ending(app_session, world, status="done", writes=()))

    runs, messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["done", "queued"], "carried over exactly once"
    assert [i.consumed_step for i in inputs] == [CARRIED_OVER_STEP]
    assert [msg.content for msg in messages if msg.role == "user"] == ["Danach bitte die Liste."]


async def test_next_turn_is_kept_out_of_the_runs_delivered_inputs(
    app_session: AppSessionFactory,
) -> None:
    """A resumed in-process leg rebuilds what the run already read; a waiting
    or cancelled `next_turn` input was never part of it."""
    from oc8.runtime.run_inputs import delivered_run_inputs

    world = await _world(app_session, messenger=False)
    await _write(app_session, world, "danach", delivery="next_turn")
    async with app_session(world.tenant) as db:
        rows = list(
            (
                await db.execute(select(m.RunInput).where(m.RunInput.run_id == world.run_id))
            ).scalars()
        )
        for row in rows:
            row.consumed_at = row.created_at
            row.consumed_step = CANCELLED_STEP
        await db.commit()
        assert await delivered_run_inputs(db, run_id=world.run_id) == []


# ------------------------- runs that end outside the executor's main branches


def _record_publish(monkeypatch: pytest.MonkeyPatch) -> list[list[ConsumedInput]]:
    published: list[list[ConsumedInput]] = []

    async def _publish(
        tenant_id: uuid.UUID, *, run_id: uuid.UUID, consumed: list[ConsumedInput]
    ) -> None:
        published.append(list(consumed))

    monkeypatch.setattr("oc8.runtime.run_inputs.publish_inputs_consumed", _publish)
    return published


async def _after_commit_events() -> None:
    """Let what `publish_after_commit` scheduled run."""
    import asyncio

    from oc8.realtime import emit

    while emit._after_commit_tasks:
        await asyncio.gather(*list(emit._after_commit_tasks))


async def _queued_input(app_session: AppSessionFactory, world: _World) -> uuid.UUID:
    return await _write(app_session, world, "Danach bitte die Liste.", delivery="next_turn")


def _raising() -> _FnRuntime:
    async def fn(db: Any, **kw: Any) -> RunResult:
        raise RuntimeError("the model provider is down")

    return _FnRuntime(fn)


async def _carried(app_session: AppSessionFactory, world: _World, *, ended: str) -> None:
    runs, messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == [ended, "queued"], "the queued message is the next turn"
    assert [i.consumed_step for i in inputs] == [CARRIED_OVER_STEP]
    assert messages[-1].role == "user" and messages[-1].content == "Danach bitte die Liste."


async def _cancelled(
    app_session: AppSessionFactory,
    world: _World,
    published: list[list[ConsumedInput]],
    *,
    ended: str,
) -> None:
    await _after_commit_events()
    runs, _messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == [ended], "no turn"
    [queued] = inputs
    assert queued.consumed_at is not None and queued.consumed_step == CANCELLED_STEP
    assert [[c.id for c in batch] for batch in published] == [[queued.id]]


async def test_a_crashed_runs_queued_message_becomes_the_next_turn(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    world = await _world(app_session, messenger=False)
    await _queued_input(app_session, world)

    await _execute(world, _raising())

    await _carried(app_session, world, ended="failed")


async def test_an_abandoned_runs_queued_message_becomes_the_next_turn(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    from sqlalchemy import update

    from oc8.runtime.reconcile import close_abandoned_runs

    world = await _world(app_session, messenger=False)
    async with app_session(world.tenant) as db:
        db.add(m.Organization(id=world.tenant, slug=f"t-{world.tenant.hex[:8]}", name="T"))
        await db.flush()
        await db.execute(
            update(m.AgentRun)
            .where(m.AgentRun.id == world.run_id)
            .values(state="running", updated_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC))
        )
        await db.commit()
    await _queued_input(app_session, world)

    assert world.run_id in await close_abandoned_runs()

    await _carried(app_session, world, ended="failed")


async def test_a_reclaimed_dead_runs_queued_message_becomes_the_next_turn(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    from sqlalchemy import update

    from oc8.runtime.executor import recover_reclaimed
    from oc8.runtime.queue import RunMessage

    world = await _world(app_session, messenger=False)
    async with app_session(world.tenant) as db:
        await db.execute(
            update(m.AgentRun)
            .where(m.AgentRun.id == world.run_id)
            .values(state="running", updated_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC))
        )
        await db.commit()
    await _queued_input(app_session, world)

    await recover_reclaimed(
        RunMessage(
            run_id=str(world.run_id), tenant_id=str(world.tenant), entry_id="0-0", redelivered=True
        )
    )

    await _carried(app_session, world, ended="failed")


async def test_a_run_cancelled_while_queued_cancels_its_queued_message(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = _record_publish(monkeypatch)
    world = await _world(app_session, messenger=False)
    await _queued_input(app_session, world)
    async with app_session(world.tenant) as db:
        db.add(
            m.RunCancellation(
                tenant_id=world.tenant,
                run_id=world.run_id,
                requested_at=dt.datetime.now(tz=dt.UTC),
                cancellation_kind="operator_interrupted",
            )
        )
        await db.commit()

    await _execute(world, _raising())

    await _cancelled(app_session, world, published, ended="interrupted")


async def test_a_run_of_an_archived_agent_cancels_its_queued_message(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = _record_publish(monkeypatch)
    world = await _world(app_session, messenger=False)
    await _queued_input(app_session, world)
    async with app_session(world.tenant) as db:
        run = await db.get(m.AgentRun, world.run_id)
        assert run is not None
        agent = await db.get(m.Agent, run.agent_id)
        assert agent is not None
        agent.deleted_at = dt.datetime.now(tz=dt.UTC)
        await db.commit()

    await _execute(world, _raising())

    await _cancelled(app_session, world, published, ended="interrupted")


async def test_a_run_of_an_inactive_organisation_cancels_its_queued_message(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = _record_publish(monkeypatch)
    world = await _world(app_session, messenger=False)
    await _queued_input(app_session, world)

    async def _inactive(_tenant_id: uuid.UUID) -> bool:
        return False

    monkeypatch.setattr("oc8.runtime.executor.tenant_may_run", _inactive)

    await _execute(world, _raising())

    await _cancelled(app_session, world, published, ended="interrupted")


async def test_a_queued_message_is_refused_on_a_run_that_is_not_a_chat_turn(
    app_session: AppSessionFactory,
) -> None:
    """Only a chat turn has a next turn to become: anywhere else a queued
    message would wait for ever."""
    from oc8.runtime.run_inputs import InputRefused

    world = await _world(app_session, messenger=False)
    async with app_session(world.tenant) as db:
        run = await db.get(m.AgentRun, world.run_id)
        assert run is not None
        run.source = "cron"
        await db.commit()

    with pytest.raises(InputRefused) as refused:
        await _queued_input(app_session, world)
    assert refused.value.status_code == 409
