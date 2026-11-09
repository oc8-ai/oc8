"""Ruling 59: no message written to a running chat run disappears silently.

What the person wrote and a run that ended DONE never read becomes ONE new
turn of the same chat, through the same door. A stopped run's inputs are not
replayed (stop means stop); a failed run's stay "not read" (its failure is the
answer)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.engine import RunResult
from oc8.constants import ACME_TENANT_ID
from oc8.runtime.executor import execute_run
from oc8.runtime.queue import RunMessage
from oc8.runtime.repository import RunRepository
from oc8.runtime.run_inputs import CARRIED_OVER_STEP, InputRefused, add_run_input
from tests.conftest import AppSessionFactory
from tests.runtime.test_executor import _FnRuntime, _RecordingChannels

pytestmark = pytest.mark.asyncio


async def _chat_run(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, messenger: bool
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A chat run of `pat` in her own chat: a web turn (she acts) or a
    messenger turn (on her behalf). Returns (run_id, session_id, member_id)."""
    async with app_session(tenant) as s:
        subject = f"pat-{uuid.uuid4().hex[:8]}"
        member = m.OrgMember(
            tenant_id=tenant, subject=subject, all_departments=True, subject_uuid=uuid.uuid4()
        )
        agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Dev")
        s.add_all([member, agent])
        await s.flush()
        session = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=member.id)
        s.add(session)
        await s.flush()
        context: dict[str, Any] = {"task": "hi", "chat_session_id": str(session.id)}
        if messenger:
            context.update(
                chat_channel="telegram", chat_channel_external_id="555", on_behalf_of=subject
            )
        else:
            context["originating_operator"] = subject
        run = await RunRepository(s).create(
            tenant_id=tenant, agent_id=agent.id, context=context, source="chat"
        )
        return run.id, session.id, member.id


def _runner(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    run_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    status: str,
    texts: tuple[str, ...] = ("Nur die aus Berlin, bitte.",),
) -> _FnRuntime:
    """A run that is written to on its last step and ends without reading it."""

    async def fn(db: Any, **kw: Any) -> RunResult:
        for text in texts:
            async with app_session(tenant) as w:
                run = await w.get(m.AgentRun, run_id)
                member = await w.get(m.OrgMember, member_id)
                assert run is not None and member is not None
                await add_run_input(w, run=run, member=member, text=text)
                await w.commit()
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=kw["agent"].id,
            status=status,
            output="Es sind 3 offen.",
            tool_calls=[],
            steps=1,
        )

    return _FnRuntime(fn)


async def _execute(run_id: uuid.UUID, tenant: uuid.UUID, runtime: _FnRuntime) -> None:
    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=runtime,
    )


async def _state(
    app_session: AppSessionFactory, tenant: uuid.UUID, session_id: uuid.UUID
) -> tuple[list[m.AgentRun], list[m.ChatMessage], list[m.RunInput]]:
    async with app_session(tenant) as db:
        runs = list(
            (
                await db.execute(
                    select(m.AgentRun)
                    .where(m.AgentRun.context["chat_session_id"].astext == str(session_id))
                    .order_by(m.AgentRun.created_at)
                )
            ).scalars()
        )
        messages = list(
            (
                await db.execute(
                    select(m.ChatMessage)
                    .where(m.ChatMessage.session_id == session_id)
                    .order_by(m.ChatMessage.created_at, m.ChatMessage.id)
                )
            ).scalars()
        )
        inputs = list(
            (
                await db.execute(
                    select(m.RunInput)
                    .where(m.RunInput.run_id.in_([r.id for r in runs]))
                    .order_by(m.RunInput.seq)
                )
            ).scalars()
        )
    return runs, messages, inputs


async def test_a_web_runs_unread_inputs_become_one_new_turn(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, session_id, member_id = await _chat_run(app_session, tenant, messenger=False)

    await _execute(
        run_id,
        tenant,
        _runner(
            app_session,
            tenant,
            run_id,
            member_id,
            status="done",
            texts=("Nur die aus Berlin.", "Und sortiert."),
        ),
    )

    runs, messages, inputs = await _state(app_session, tenant, session_id)
    assert [r.state for r in runs] == ["done", "queued"], "exactly one follow-up run"
    first, follow = runs
    assert [(i.consumed_step, i.consumed_at is not None) for i in inputs] == [
        (CARRIED_OVER_STEP, True),
        (CARRIED_OVER_STEP, True),
    ]
    turn = messages[-1]
    assert turn.role == "user" and turn.author_member_id == member_id
    assert turn.content == "Nur die aus Berlin.\n\nUnd sortiert."
    assert [msg.role for msg in messages] == ["assistant", "user"], "after the answer"
    assert follow.context["reply_to_message_id"] == str(turn.id)
    assert follow.context["originating_operator"] == first.context["originating_operator"]
    assert "operator_role" not in follow.context, "never a borrowed token role"
    assert follow.context["door"] == "web" and "chat_channel" not in follow.context


async def test_a_messenger_runs_unread_input_becomes_a_messenger_turn(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, session_id, member_id = await _chat_run(app_session, tenant, messenger=True)
    fake = _RecordingChannels(app_session, tenant, run_id)
    monkeypatch.setattr("oc8.channels.registry.channels_for_tenant", fake._channels_for_tenant)

    await _execute(run_id, tenant, _runner(app_session, tenant, run_id, member_id, status="done"))

    runs, messages, inputs = await _state(app_session, tenant, session_id)
    assert [r.state for r in runs] == ["done", "queued"]
    first, follow = runs
    assert [i.consumed_step for i in inputs] == [CARRIED_OVER_STEP]
    assert messages[-1].content == "Nur die aus Berlin, bitte."
    assert follow.context["chat_channel"] == "telegram"
    assert follow.context["chat_channel_external_id"] == "555"
    assert follow.context["door"] == "telegram"
    assert follow.context["on_behalf_of"] == first.context["on_behalf_of"]
    assert "originating_operator" not in follow.context
    # The finished run's own answer still reached the sender first.
    assert fake.said == [("555", "Es sind 3 offen.")]


async def test_a_stopped_runs_inputs_are_not_replayed(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, session_id, member_id = await _chat_run(app_session, tenant, messenger=False)

    await _execute(
        run_id, tenant, _runner(app_session, tenant, run_id, member_id, status="interrupted")
    )

    runs, _messages, inputs = await _state(app_session, tenant, session_id)
    assert [r.state for r in runs] == ["interrupted"], "stop means stop"
    assert [(i.consumed_at, i.consumed_step) for i in inputs] == [(None, None)]


async def test_a_failed_runs_inputs_stay_unread_and_start_nothing(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, session_id, member_id = await _chat_run(app_session, tenant, messenger=False)

    await _execute(run_id, tenant, _runner(app_session, tenant, run_id, member_id, status="failed"))

    runs, _messages, inputs = await _state(app_session, tenant, session_id)
    assert [r.state for r in runs] == ["failed"]
    assert [(i.consumed_at, i.consumed_step) for i in inputs] == [(None, None)]


async def test_a_run_without_unread_inputs_starts_no_turn(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, session_id, member_id = await _chat_run(app_session, tenant, messenger=False)

    await _execute(
        run_id, tenant, _runner(app_session, tenant, run_id, member_id, status="done", texts=())
    )

    runs, _messages, _inputs = await _state(app_session, tenant, session_id)
    assert [r.state for r in runs] == ["done"]


async def test_an_input_to_a_run_that_ended_meanwhile_is_refused(
    app_session: AppSessionFactory,
) -> None:
    """The writer read the run while it ran; it ended before the write. Asked
    again under the lock, from the database: 409, nothing written."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    run_id, _session_id, member_id = await _chat_run(app_session, tenant, messenger=False)
    async with app_session(tenant) as writer:
        run = await writer.get(m.AgentRun, run_id)
        member = await writer.get(m.OrgMember, member_id)
        assert run is not None and member is not None and run.state == "queued"
        async with app_session(tenant) as executor:
            ended = await executor.get(m.AgentRun, run_id)
            assert ended is not None
            ended.state = "done"
            await executor.commit()
        with pytest.raises(InputRefused) as refused:
            await add_run_input(writer, run=run, member=member, text="zu spät")
        assert refused.value.status_code == 409
        count = (await writer.execute(select(m.RunInput).where(m.RunInput.run_id == run_id))).all()
        assert count == []
