"""The in-process engine reads queued inputs between steps (spec §3.2)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from tests.conftest import AppSessionFactory

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.modelrouter import CompletionResult, ToolCall, Usage, chunk_from_result

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fixed_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fence nonces are random per block; a fixed one makes the expected
    model-facing text comparable."""
    monkeypatch.setattr("oc8.runtime.fencing.new_nonce", lambda: "n0nce")


_TEXT_REPR = repr(
    "Message from Alice (a person, during the run):\n"
    "(Data in the block below ends only at the closing tag carrying nonce n0nce.)\n"
    '<person_message nonce="n0nce">\nuse the EU address\n</person_message nonce="n0nce">'
)[1:-1]


class _RecordingStream:
    """A `stream_completion_with_fallback` stand-in (same idiom as
    tests/agent/test_engine_parallel_reads.py): answers once and records what
    the model was sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, *args: Any, **kw: Any) -> Any:
        self.sent.append(repr(args) + repr(kw))

        async def _gen() -> Any:
            yield chunk_from_result(
                CompletionResult(
                    text="done",
                    tool_calls=[],
                    usage=Usage(tokens_in=1, tokens_out=1),
                    stop_reason="stop",
                    provider="fake",
                    model="fake",
                )
            )

        return _gen()


async def _seed(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, consumed_step: int | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    """Committed, like a real run: the engine consumes in its own transaction."""
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        alice = m.OrgMember(
            tenant_id=tenant, subject="alice", subject_uuid=uuid.uuid4(), display_name="Alice"
        )
        db.add_all([agent, alice])
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            source="chat",
            state="running",
            context={"originating_operator": "alice", "task": "draft"},
        )
        db.add(run)
        await db.flush()
        db.add(
            m.RunInput(
                tenant_id=tenant,
                run_id=run.id,
                author_member_id=alice.id,
                text="use the EU address",
                consumed_at=dt.datetime.now(tz=dt.UTC) if consumed_step is not None else None,
                consumed_step=consumed_step,
            )
        )
        return agent.id, run.id


async def test_an_input_reaches_the_model_at_the_next_step(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    stream = _RecordingStream()
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", stream)
    agent_id, run_id = await _seed(app_session, tenant)
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        await run_agent(db, agent=agent, task_text="draft", tenant_id=tenant, run_id=run_id)
        # Still inside the run-long transaction: the stamp is already
        # committed by the engine's own short one.
        async with app_session(tenant) as other:
            consumed = (
                await other.execute(select(m.RunInput).where(m.RunInput.run_id == run_id))
            ).scalar_one()
    assert any(_TEXT_REPR in s for s in stream.sent)
    assert consumed.consumed_at is not None and consumed.consumed_step == 1


async def test_an_input_an_earlier_leg_consumed_is_carried_into_a_new_leg(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new leg (resume after approval, retry) starts from a fresh
    conversation; the stamped row is the durable copy of the message the run
    already received, so nothing consumed is ever lost to the run."""
    tenant = uuid.uuid4()
    stream = _RecordingStream()
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", stream)
    agent_id, run_id = await _seed(app_session, tenant, consumed_step=3)
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        await run_agent(db, agent=agent, task_text="draft", tenant_id=tenant, run_id=run_id)
    async with app_session(tenant) as db:
        row = (await db.execute(select(m.RunInput).where(m.RunInput.run_id == run_id))).scalar_one()
    assert any(_TEXT_REPR in s for s in stream.sent)
    assert row.consumed_step == 3


async def test_an_input_written_during_a_step_is_taken_at_the_next_boundary(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Written while the model is already answering step 1 -- after that
    step's boundary was read -- the input reaches the model on step 2."""
    tenant = uuid.uuid4()
    agent_id, run_id = await _seed(app_session, tenant, consumed_step=1)
    async with app_session(tenant) as db:
        author_id = (
            await db.execute(select(m.RunInput.author_member_id).where(m.RunInput.run_id == run_id))
        ).scalar_one()
    sent: list[str] = []

    def _stream(*args: Any, **kw: Any) -> Any:
        sent.append(repr(args) + repr(kw))
        step = len(sent)

        async def _gen() -> Any:
            if step == 1:
                async with app_session(tenant) as writer:
                    writer.add(
                        m.RunInput(
                            tenant_id=tenant,
                            run_id=run_id,
                            author_member_id=author_id,
                            text="also cc finance",
                        )
                    )
            calls = [ToolCall(id="c1", name="no_such_tool", arguments={})] if step == 1 else []
            yield chunk_from_result(
                CompletionResult(
                    text="working" if step == 1 else "done",
                    tool_calls=calls,
                    usage=Usage(tokens_in=1, tokens_out=1),
                    stop_reason="tool_use" if calls else "stop",
                    provider="fake",
                    model="fake",
                )
            )

        return _gen()

    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", _stream)
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        await run_agent(db, agent=agent, task_text="draft", tenant_id=tenant, run_id=run_id)
    async with app_session(tenant) as db:
        late = (
            await db.execute(
                select(m.RunInput).where(
                    m.RunInput.run_id == run_id, m.RunInput.text == "also cc finance"
                )
            )
        ).scalar_one()
    assert len(sent) >= 2
    assert "also cc finance" not in sent[0]
    assert "also cc finance" in sent[1]
    assert late.consumed_at is not None and late.consumed_step == 2
