"""Rulings 59-61: no message written to a running chat run disappears silently.

What the person wrote and a run that ended DONE never read becomes ONE new
turn of the same chat, through the same door, staged in the run's terminal
transaction (Ruling 61) and only if the door's checks still pass. A stopped
run's inputs are not replayed (stop means stop); a failed run's stay "not
read", and a messenger sender is told. Writers and the executor take the
tenant audit lock before the run_input lock (Ruling 60)."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.engine import RunResult
from oc8.audit import append_event
from oc8.authz.permissions import ORG_ADMIN
from oc8.channels.binding import issue_code, redeem_code
from oc8.runtime.executor import _UNREAD_AFTER_FAILURE, execute_run
from oc8.runtime.queue import RunMessage
from oc8.runtime.repository import RunRepository
from oc8.runtime.run_inputs import CARRIED_OVER_STEP, InputRefused, add_run_input
from tests.conftest import AppSessionFactory
from tests.runtime.test_executor import _FnRuntime, _RecordingChannels

pytestmark = pytest.mark.asyncio


class _World:
    def __init__(self, tenant: uuid.UUID) -> None:
        self.tenant = tenant
        self.run_id = uuid.uuid4()
        self.session_id = uuid.uuid4()
        self.member_id = uuid.uuid4()
        self.binding_id: uuid.UUID | None = None


async def _world(app_session: AppSessionFactory, *, messenger: bool) -> _World:
    """`pat`, an org admin, and a chat run of hers in her own chat: a web turn
    (she acts, with her live token role recorded) or a messenger turn (on her
    behalf, from her bound account; the run's stored chat id is stale)."""
    world = _World(uuid.uuid4())
    tenant = world.tenant
    code = ""
    async with app_session(tenant) as s:
        role = m.Role(tenant_id=tenant, name=ORG_ADMIN, builtin=True, kind="human")
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        s.add_all([role, dept])
        await s.flush()
        member = m.OrgMember(
            tenant_id=tenant,
            subject="pat",
            all_departments=True,
            subject_uuid=uuid.uuid4(),
            role_id=role.id,
        )
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Dev")
        s.add_all([member, agent])
        await s.flush()
        world.member_id = member.id
        session = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=member.id)
        s.add(session)
        await s.flush()
        world.session_id = session.id
        context: dict[str, Any] = {"task": "hi", "chat_session_id": str(session.id)}
        if messenger:
            row = await issue_code(
                s, tenant_id=tenant, user_id=uuid.uuid4(), channel="telegram", member_id=member.id
            )
            code = str(row.code)
            context.update(
                chat_channel="telegram", chat_channel_external_id="stale", on_behalf_of="pat"
            )
        else:
            context.update(
                originating_operator="pat", operator_role="org_admin", acting_token_role="org_admin"
            )
        run = await RunRepository(s).create(
            tenant_id=tenant, agent_id=agent.id, context=context, source="chat"
        )
        world.run_id = run.id
        await s.commit()
    if messenger:
        async with app_session(tenant) as s:
            bound = await redeem_code(
                s, tenant_id=tenant, channel="telegram", code=code, external_id="777"
            )
            world.binding_id = bound.id
            await s.commit()
    return world


async def _write(app_session: AppSessionFactory, world: _World, text: str) -> None:
    async with app_session(world.tenant) as w:
        run = await w.get(m.AgentRun, world.run_id)
        member = await w.get(m.OrgMember, world.member_id)
        assert run is not None and member is not None
        await add_run_input(w, run=run, member=member, text=text)
        await w.commit()


def _runner(
    app_session: AppSessionFactory,
    world: _World,
    *,
    status: str,
    texts: tuple[str, ...] = ("Nur die aus Berlin, bitte.",),
    meanwhile: Callable[[], Awaitable[None]] | None = None,
) -> _FnRuntime:
    """A run written to on its last step that ends without reading it."""

    async def fn(db: Any, **kw: Any) -> RunResult:
        for text in texts:
            await _write(app_session, world, text)
        if meanwhile is not None:
            await meanwhile()
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status=status,
            output="Es sind 3 offen.",
            tool_calls=[],
            steps=1,
        )

    return _FnRuntime(fn)


async def _execute(world: _World, runtime: _FnRuntime) -> None:
    await execute_run(
        RunMessage(
            run_id=str(world.run_id), tenant_id=str(world.tenant), entry_id="0-0", redelivered=False
        ),
        runtime=runtime,
    )


async def _state(
    app_session: AppSessionFactory, world: _World
) -> tuple[list[m.AgentRun], list[m.ChatMessage], list[m.RunInput]]:
    async with app_session(world.tenant) as db:
        runs = list(
            (
                await db.execute(
                    select(m.AgentRun)
                    .where(m.AgentRun.context["chat_session_id"].astext == str(world.session_id))
                    .order_by(m.AgentRun.created_at)
                )
            ).scalars()
        )
        messages = list(
            (
                await db.execute(
                    select(m.ChatMessage)
                    .where(m.ChatMessage.session_id == world.session_id)
                    .order_by(m.ChatMessage.created_at, m.ChatMessage.id)
                )
            ).scalars()
        )
        inputs = list(
            (
                await db.execute(
                    select(m.RunInput)
                    .where(m.RunInput.run_id == world.run_id)
                    .order_by(m.RunInput.seq)
                )
            ).scalars()
        )
    return runs, messages, inputs


def _unread(inputs: list[m.RunInput]) -> bool:
    return bool(inputs) and all(i.consumed_at is None and i.consumed_step is None for i in inputs)


async def test_a_web_runs_unread_inputs_become_one_new_turn(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    world = await _world(app_session, messenger=False)

    await _execute(
        world,
        _runner(app_session, world, status="done", texts=("Nur die aus Berlin.", "Und sortiert.")),
    )

    runs, messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["done", "queued"], "exactly one follow-up run"
    follow = runs[1]
    assert [i.consumed_step for i in inputs] == [CARRIED_OVER_STEP, CARRIED_OVER_STEP]
    turn = messages[-1]
    assert [msg.role for msg in messages] == ["assistant", "user"], "after the answer"
    assert turn.author_member_id == world.member_id
    assert turn.content == "Nur die aus Berlin.\n\nUnd sortiert."
    assert follow.context["reply_to_message_id"] == str(turn.id)
    assert follow.context["originating_operator"] == "pat"
    assert "operator_role" not in follow.context, "never a borrowed token role"
    assert "acting_token_role" not in follow.context
    assert follow.context["door"] == "web" and "chat_channel" not in follow.context


async def test_a_messenger_runs_unread_input_becomes_a_messenger_turn(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(app_session, messenger=True)
    fake = _RecordingChannels(app_session, world.tenant, world.run_id)
    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", fake._channels_for_tenant)

    await _execute(world, _runner(app_session, world, status="done"))

    runs, messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["done", "queued"]
    follow = runs[1]
    assert [i.consumed_step for i in inputs] == [CARRIED_OVER_STEP]
    assert messages[-1].content == "Nur die aus Berlin, bitte."
    assert follow.context["chat_channel"] == "telegram"
    assert follow.context["chat_channel_external_id"] == "777", "from the live binding"
    assert follow.context["door"] == "telegram"
    assert follow.context["on_behalf_of"] == "pat"
    assert "originating_operator" not in follow.context
    assert fake.said == [("stale", "Es sind 3 offen.")], "the finished run's own answer"


async def test_a_stopped_runs_inputs_are_not_replayed(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    world = await _world(app_session, messenger=False)

    await _execute(world, _runner(app_session, world, status="interrupted"))

    runs, _messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["interrupted"], "stop means stop"
    assert _unread(inputs)


async def test_a_failed_messenger_run_says_once_that_the_message_did_not_arrive(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(app_session, messenger=True)
    fake = _RecordingChannels(app_session, world.tenant, world.run_id)
    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", fake._channels_for_tenant)

    await _execute(world, _runner(app_session, world, status="failed"))

    runs, _messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["failed"], "no turn"
    assert _unread(inputs)
    assert [text for _ext, text in fake.said].count(_UNREAD_AFTER_FAILURE) == 1


async def test_a_failed_run_without_unread_inputs_says_nothing_more(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(app_session, messenger=True)
    fake = _RecordingChannels(app_session, world.tenant, world.run_id)
    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", fake._channels_for_tenant)

    await _execute(world, _runner(app_session, world, status="failed", texts=()))

    assert _UNREAD_AFTER_FAILURE not in [text for _ext, text in fake.said]


async def test_a_run_without_unread_inputs_starts_no_turn(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    world = await _world(app_session, messenger=False)

    await _execute(world, _runner(app_session, world, status="done", texts=()))

    runs, _messages, _inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["done"]


# ------------------------------------------------- the door's checks, again


async def _refused(app_session: AppSessionFactory, world: _World) -> None:
    runs, messages, inputs = await _state(app_session, world)
    assert [r.state for r in runs] == ["done"], "nothing started"
    assert [msg.role for msg in messages] == ["assistant"]
    assert _unread(inputs), "left unread: the UI says the run finished before it read it"


async def test_an_offboarded_owners_inputs_start_nothing(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    world = await _world(app_session, messenger=False)

    async def offboard() -> None:
        async with app_session(world.tenant) as db:
            member = await db.get(m.OrgMember, world.member_id)
            assert member is not None
            member.deleted_at = dt.datetime.now(tz=dt.UTC)
            await db.commit()

    await _execute(world, _runner(app_session, world, status="done", meanwhile=offboard))

    await _refused(app_session, world)


async def test_an_owner_who_lost_the_agent_starts_nothing(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    world = await _world(app_session, messenger=False)

    async def lose_reach() -> None:
        async with app_session(world.tenant) as db:
            member = await db.get(m.OrgMember, world.member_id)
            assert member is not None
            member.role_id = None
            member.all_departments = False
            await db.commit()

    # Her recorded token role is not used for this check: start without one
    # to fail closed.
    async with app_session(world.tenant) as db:
        run = await db.get(m.AgentRun, world.run_id)
        assert run is not None
        run.context = {k: v for k, v in run.context.items() if k != "acting_token_role"}
        await db.commit()

    await _execute(world, _runner(app_session, world, status="done", meanwhile=lose_reach))

    await _refused(app_session, world)


async def test_a_revoked_binding_starts_nothing(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(app_session, messenger=True)
    fake = _RecordingChannels(app_session, world.tenant, world.run_id)
    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", fake._channels_for_tenant)

    async def revoke() -> None:
        async with app_session(world.tenant) as db:
            binding = await db.get(m.ApprovalChannelBinding, world.binding_id)
            assert binding is not None
            binding.revoked_at = dt.datetime.now(tz=dt.UTC)
            await db.commit()

    await _execute(world, _runner(app_session, world, status="done", meanwhile=revoke))

    await _refused(app_session, world)


# ------------------------------------------------------------------ races


async def test_an_input_to_a_run_that_ended_meanwhile_is_refused(
    app_session: AppSessionFactory,
) -> None:
    """The writer read the run while it ran; it ended before the write. Asked
    again under the lock, from the database: 409, nothing written."""
    world = await _world(app_session, messenger=False)
    async with app_session(world.tenant) as writer:
        run = await writer.get(m.AgentRun, world.run_id)
        member = await writer.get(m.OrgMember, world.member_id)
        assert run is not None and member is not None and run.state == "queued"
        async with app_session(world.tenant) as executor:
            ended = await executor.get(m.AgentRun, world.run_id)
            assert ended is not None
            ended.state = "done"
            await executor.commit()
        with pytest.raises(InputRefused) as refused:
            await add_run_input(writer, run=run, member=member, text="zu spät")
        assert refused.value.status_code == 409 and refused.value.code == "finished"
        rows = (
            await writer.execute(select(m.RunInput).where(m.RunInput.run_id == world.run_id))
        ).all()
        assert rows == []


async def test_a_write_racing_the_end_of_an_auditing_run_does_not_deadlock(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """Regression (Ruling 60): the executor's transaction holds the tenant's
    audit lock (it appended an event during the run) when it reaches the end
    and takes the run_input lock; a web write arrives at that moment. With the
    writer taking run_input first and audit second, the two waited on each
    other. Now both take audit first: the write waits for the end, then is
    refused (409) -- or, had it won, would have been carried over."""
    world = await _world(app_session, messenger=False)
    outcome: dict[str, Any] = {}

    async def write() -> None:
        try:
            await _write(app_session, world, "Noch schnell: nur Berlin.")
            outcome["written"] = True
        except InputRefused as exc:
            outcome["refused"] = exc.code

    async def fn(db: Any, **kw: Any) -> RunResult:
        await append_event(
            db,
            tenant_id=world.tenant,
            actor_type="agent",
            actor_id=kw["agent"].id,
            category="run",
            action="run.tool_called",
            resource={"run_id": str(world.run_id)},
        )
        outcome["task"] = asyncio.create_task(write())
        # Long enough for the writer to be inside `add_run_input`, waiting.
        await asyncio.sleep(0.5)
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="Fertig.",
            tool_calls=[],
            steps=1,
        )

    await asyncio.wait_for(_execute(world, _FnRuntime(fn)), timeout=30)
    await asyncio.wait_for(outcome["task"], timeout=30)

    runs, messages, inputs = await _state(app_session, world)
    assert runs[0].state == "done"
    if outcome.get("refused"):
        assert outcome["refused"] == "finished" and inputs == []
        assert [r.state for r in runs] == ["done"]
    else:
        assert outcome.get("written") is True
        assert [i.consumed_step for i in inputs] == [CARRIED_OVER_STEP]
        assert messages[-1].content == "Noch schnell: nur Berlin."


async def _runner_that_caches_the_owner(
    app_session: AppSessionFactory,
    world: _World,
    change: Callable[[], Awaitable[None]],
) -> _FnRuntime:
    """A run that loads the chat and its owner into its OWN session first
    (as `_resolve_agent_actor` and `_current_chat_sender` do), is written to,
    and then the owner changes in another transaction before the run ends."""

    held: list[Any] = []

    async def fn(db: Any, **kw: Any) -> RunResult:
        session = await db.get(m.ChatSession, world.session_id)
        member = await db.get(m.OrgMember, world.member_id)
        assert session is not None and member is not None
        assert member.deleted_at is None and member.role_id is not None
        # Kept referenced until the run ends, as a run's own objects are: the
        # session's identity map holds rows only while something does.
        held.extend([session, member])
        await _write(app_session, world, "Nur die aus Berlin, bitte.")
        await change()
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="Es sind 3 offen.",
            tool_calls=[],
            steps=1,
        )

    return _FnRuntime(fn)


async def test_an_owner_offboarded_after_the_run_loaded_her_starts_nothing(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    world = await _world(app_session, messenger=False)

    async def offboard() -> None:
        async with app_session(world.tenant) as db:
            member = await db.get(m.OrgMember, world.member_id)
            assert member is not None
            member.deleted_at = dt.datetime.now(tz=dt.UTC)
            await db.commit()

    await _execute(world, await _runner_that_caches_the_owner(app_session, world, offboard))

    await _refused(app_session, world)


async def test_an_owner_demoted_after_the_run_loaded_her_starts_nothing(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    world = await _world(app_session, messenger=False)
    async with app_session(world.tenant) as db:
        run = await db.get(m.AgentRun, world.run_id)
        assert run is not None
        run.context = {k: v for k, v in run.context.items() if k != "acting_token_role"}
        await db.commit()

    async def demote() -> None:
        async with app_session(world.tenant) as db:
            member = await db.get(m.OrgMember, world.member_id)
            assert member is not None
            member.role_id = None
            member.all_departments = False
            await db.commit()

    await _execute(world, await _runner_that_caches_the_owner(app_session, world, demote))

    await _refused(app_session, world)
