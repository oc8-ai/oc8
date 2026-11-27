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


async def _asked(app_session: AppSessionFactory, world: _World) -> None:
    """The user turn the run answers, in the chat's tree."""
    from oc8.chat import tree

    async with app_session(world.tenant) as db:
        session = await db.get(m.ChatSession, world.session_id)
        run = await db.get(m.AgentRun, world.run_id)
        assert session is not None and run is not None
        asked = m.ChatMessage(
            tenant_id=world.tenant,
            session_id=session.id,
            role="user",
            content="Wie viele sind offen?",
            author_member_id=world.member_id,
        )
        await tree.append_person_write(db, session=session, message=asked)
        run.context = {**run.context, "reply_to_message_id": str(asked.id)}
        await db.commit()


def _record_carry_events(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[list[ConsumedInput]], list[uuid.UUID]]:
    published = _record_publish(monkeypatch)
    started: list[uuid.UUID] = []

    async def _publish_run(*, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        started.append(run_id)

    monkeypatch.setattr("oc8.runtime.intake.publish_run", _publish_run)
    return published, started


async def _answered_then_carried(
    app_session: AppSessionFactory,
    world: _World,
    published: list[list[ConsumedInput]],
    started: list[uuid.UUID],
) -> None:
    """The run's own failure answer, then the queued message as the next
    turn -- announced and started after commit."""
    await _after_commit_events()
    await _carried(app_session, world, ended="failed")
    runs, messages, inputs = await _state(app_session, world)
    assert [msg.role for msg in messages] == ["user", "assistant", "user"]
    assert "didn't work out" in messages[1].content
    assert [[c.id for c in batch] for batch in published] == [[inputs[0].id]]
    assert published[0][0].step == CARRIED_OVER_STEP
    assert started == [runs[1].id]


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
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(app_session, messenger=False)
    await _asked(app_session, world)
    await _queued_input(app_session, world)
    published, started = _record_carry_events(monkeypatch)

    await _execute(world, _raising())

    await _answered_then_carried(app_session, world, published, started)


async def test_an_abandoned_runs_queued_message_becomes_the_next_turn(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import update

    from oc8.runtime.reconcile import close_abandoned_runs

    world = await _world(app_session, messenger=False)
    await _asked(app_session, world)
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
    published, started = _record_carry_events(monkeypatch)

    assert world.run_id in await close_abandoned_runs()

    await _answered_then_carried(app_session, world, published, started)


async def test_a_reclaimed_dead_runs_queued_message_becomes_the_next_turn(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import update

    from oc8.runtime.executor import recover_reclaimed
    from oc8.runtime.queue import RunMessage

    world = await _world(app_session, messenger=False)
    await _asked(app_session, world)
    async with app_session(world.tenant) as db:
        await db.execute(
            update(m.AgentRun)
            .where(m.AgentRun.id == world.run_id)
            .values(state="running", updated_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC))
        )
        await db.commit()
    await _queued_input(app_session, world)
    published, started = _record_carry_events(monkeypatch)

    await recover_reclaimed(
        RunMessage(
            run_id=str(world.run_id), tenant_id=str(world.tenant), entry_id="0-0", redelivered=True
        )
    )

    await _answered_then_carried(app_session, world, published, started)


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


async def test_a_crash_after_somebody_else_failed_the_run_carries_nothing_over(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """The reconciler failed the run while its runtime was still working:
    that ending stands, with its own answer and carry-over. The executor
    coming back with a crash neither carries the queued message over a
    second time nor records a second failure answer."""
    from sqlalchemy import update

    world = await _world(app_session, messenger=False)
    await _asked(app_session, world)
    await _queued_input(app_session, world)

    async def fn(db: Any, **kw: Any) -> RunResult:
        # Somebody else's verdict, as the database has it.
        await db.execute(
            update(m.AgentRun).where(m.AgentRun.id == world.run_id).values(state="failed")
        )
        raise RuntimeError("the model provider is down")

    await _execute(world, _FnRuntime(fn))

    runs, messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["failed"], "no second turn"
    assert [msg.role for msg in messages] == ["user"], "no failure answer of this executor's"
    assert [i.consumed_at for i in inputs] == [None]


async def test_a_late_answer_after_somebody_else_failed_the_run_is_not_recorded(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconciler failed the run (its failure answer, the queued message
    carried over) while the runtime was still working; the runtime then
    returns an answer. That ending stands: no late answer after the carried
    turn, no second reply on the messenger."""
    from sqlalchemy import update

    from oc8.runtime.executor import _CHAT_RUN_FAILED, answer_failed_chat_end
    from oc8.runtime.run_inputs import lock_run_inputs
    from tests.runtime.test_executor import _RecordingChannels

    world = await _world(app_session, messenger=True)
    fake = _RecordingChannels(app_session, world.tenant, world.run_id)
    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", fake._channels_for_tenant)
    await _asked(app_session, world)
    await _queued_input(app_session, world)

    async def fn(db: Any, **kw: Any) -> RunResult:
        # The reconciler's end, in the session that holds the run's row.
        await lock_run_inputs(db, tenant_id=world.tenant, run_id=world.run_id)
        await db.execute(
            update(m.AgentRun).where(m.AgentRun.id == world.run_id).values(state="failed")
        )
        run = await db.get(m.AgentRun, world.run_id)
        assert run is not None
        await answer_failed_chat_end(db, run)
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="Es sind 3 offen.",
            tool_calls=[],
            steps=1,
        )

    await _execute(world, _FnRuntime(fn))

    runs, messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["failed", "queued"], "carried over once"
    assert [msg.role for msg in messages] == ["user", "assistant", "user"], "no late answer"
    assert "Es sind 3 offen." not in [msg.content for msg in messages]
    assert [i.consumed_step for i in inputs] == [CARRIED_OVER_STEP]
    # The sender still hears that it failed, once, on the run's channel.
    assert fake.said == [("stale", _CHAT_RUN_FAILED)]


def _no_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8.runtime import executor

    async def _resolve(*_a: Any, **_kw: Any) -> Any:
        return executor._ConnectionChoice(None, "no login for this tool")

    monkeypatch.setattr(executor, "_resolve_mcp_connection", _resolve)


async def test_a_run_without_its_connection_answers_then_carries_over(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(app_session, messenger=False)
    await _asked(app_session, world)
    await _queued_input(app_session, world)
    published, started = _record_carry_events(monkeypatch)
    _no_connection(monkeypatch)

    await _execute(world, _raising())

    await _answered_then_carried(app_session, world, published, started)


async def test_a_run_without_its_connection_ended_elsewhere_answers_nothing(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(app_session, messenger=False)
    await _asked(app_session, world)
    await _queued_input(app_session, world)
    _no_connection(monkeypatch)

    async def _ended_elsewhere(*_a: Any, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr("oc8.runtime.executor._run_still_open", _ended_elsewhere)

    await _execute(world, _raising())

    runs, messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["failed"]
    assert [msg.role for msg in messages] == ["user"]
    assert [i.consumed_at for i in inputs] == [None]
