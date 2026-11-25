"""A chat run carries its turn, not its transcript (phase 1b, spec §5.1): the
conversation is read back from the tree at run start, and a runtime other
than the in-process engine gets the flattened text it always got."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.engine import RunResult
from oc8.chat import tree
from oc8.chat.carry_over import _Door, _stage_turn
from oc8.chat.modes import MODES, mode_directive
from oc8.chat.service import regenerate_reply
from oc8.chat.turn_context import NOTICE_PREFIX, flattened_task_text
from oc8.runtime.adapter import EchoRuntimeStub, Oc8AgentRuntime, runs_in_process
from oc8.runtime.executor import execute_run
from oc8.runtime.queue import RunMessage
from tests.chat.test_message_tree import _commit, _parent, _reply, _send, _session
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


# Frozen copy of `chat.service._build_task_text` and `_speaker` as they were
# before the transcript left the run context: the reference the rebuilt text
# must match byte for byte.
def _old_speaker(role: str, content: str = "") -> str:
    if role == "user":
        return "User"
    if role == "followup" and content.startswith("oc8 notice: "):
        return "oc8 (a notice from the system, not typed by the user)"
    if role == "followup":
        return "Follow-up (scheduled by you, not typed by the user)"
    return "You (assistant)"


def _old_build_task_text(history: list[Any], new_message: str, new_role: str = "user") -> str:
    lines = []
    for msg in history[-20:]:
        lines.append(f"{_old_speaker(msg.role, msg.content)}: {msg.content}")
    lines.append(f"{_old_speaker(new_role, new_message)}: {new_message}")
    return "\n".join(lines)


async def _long_chat(db: Any, session: m.ChatSession) -> None:
    """25 messages: user turns and answers, a follow-up and an oc8 notice."""
    for i in range(11):
        _u, run = await _send(db, session, f"question {i}")
        await _reply(db, run, f"answer {i}")
    _f, run_f = await _send(
        db,
        session,
        "Follow-up for invoices: check them",
        role="followup",
        followup={"responsibility_id": str(uuid.uuid4())},
    )
    await _reply(db, run_f, "checked the invoices")
    await _send(
        db,
        session,
        f"{NOTICE_PREFIX}the person clicked Create on a card",
        role="followup",
        notice={"kind": "agent_refused"},
    )
    await _commit(db, session.tenant_id)


async def test_task_text_is_byte_identical_for_an_external_runtime(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        await _long_chat(db, session)
        history = await tree.visible_path(db, session=session)
        assert len(history) == 25
        _u26, run = await _send(db, session, "/plan turn twenty-six")
        old = (
            _old_build_task_text(history, "turn twenty-six")
            + "\n\n"
            + mode_directive(MODES["plan"])
        )
        assert await flattened_task_text(db, run) == old


async def test_the_run_context_holds_no_transcript(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        first, run1 = await _send(db, session, "FIRST-MESSAGE")
        assert run1.context["transcript_until"] is None, "a first turn follows nothing"
        r1 = await _reply(db, run1, "FIRST-ANSWER")
        u2, run2 = await _send(db, session, "SECOND-MESSAGE")
        context = run2.context
        assert "User: FIRST-MESSAGE" not in context["task"]
        assert "FIRST-ANSWER" not in context["task"]
        assert context["task"] == "User: SECOND-MESSAGE"
        assert context["turn_text"] == "SECOND-MESSAGE"
        assert context["turn_role"] == "user"
        assert context["transcript_until"] == str(await _parent(db, u2.id)) == str(r1.id)
        assert first.id != u2.id


async def test_regenerate_points_transcript_until_at_the_answered_turns_parent(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u1, run1 = await _send(db, session, "one")
        r1 = await _reply(db, run1, "answer one")
        u2, run2 = await _send(db, session, "two")
        await _reply(db, run2, "answer two")
        before = await tree.path_to(db, session=session, message_id=r1.id)
        run = await regenerate_reply(
            db,
            session=session,
            tenant_id=tenant,
            user_message=u2,
            originating_operator="op",
            operator_role=None,
        )
        assert run.context["transcript_until"] == str(await _parent(db, u2.id)) == str(r1.id)
        assert run.context["turn_role"] == "user"
        assert "answer one" not in run.context["task"]
        assert await flattened_task_text(db, run) == _old_build_task_text(before, "two")


async def test_carried_over_turn_sets_transcript_until(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        u1, run1 = await _send(db, session, "one")
        r1 = await _reply(db, run1, "answer one")
        agent = await db.get(m.Agent, session.agent_id)
        assert agent is not None
        run_id = await _stage_turn(
            db,
            session=session,
            member_id=session.member_id,
            agent=agent,
            door=_Door(
                originating_operator="op",
                for_person=None,
                chat_channel=None,
                chat_channel_external_id=None,
            ),
            text="and also this",
            anchor_id=u1.id,
        )
        await _commit(db, tenant)
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.context["transcript_until"] == str(r1.id)
        assert run.context["task"] == "User: and also this"
        before = await tree.path_to(db, session=session, message_id=r1.id)
        assert await flattened_task_text(db, run) == _old_build_task_text(before, "and also this")


async def test_a_non_chat_run_is_untouched(app_session: AppSessionFactory, redis_url: str) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        trigger_run = m.AgentRun(
            tenant_id=tenant, agent_id=session.agent_id, context={"task": "Check the inbox."}
        )
        # A delegation wake-up keeps its chat but carries no turn of its own.
        wake = m.AgentRun(
            tenant_id=tenant,
            agent_id=session.agent_id,
            context={"task": "Sub-task done: 3 rows", "chat_session_id": str(session.id)},
        )
        db.add_all([trigger_run, wake])
        await db.flush()
        assert await flattened_task_text(db, trigger_run) == "Check the inbox."
        assert await flattened_task_text(db, wake) == "Sub-task done: 3 rows"


def test_only_the_in_process_engine_runs_in_process() -> None:
    assert runs_in_process(Oc8AgentRuntime())
    assert not runs_in_process(EchoRuntimeStub())


class _Recording:
    def __init__(self, task_id: uuid.UUID) -> None:
        self.task_id = task_id
        self.task_text: str | None = None

    async def execute(self, db: Any, **kw: Any) -> RunResult:
        self.task_text = kw["task_text"]
        return RunResult(task_id=self.task_id, agent_id=kw["agent"].id, status="done", output="ok")


class _InProcessRecording(Oc8AgentRuntime):
    def __init__(self, task_id: uuid.UUID) -> None:
        self.task_id = task_id
        self.task_text: str | None = None

    async def execute(self, db: Any, **kw: Any) -> RunResult:  # type: ignore[override]
        self.task_text = kw["task_text"]
        return RunResult(task_id=self.task_id, agent_id=kw["agent"].id, status="done", output="ok")


@pytest.mark.parametrize("in_process", [False, True])
async def test_the_executor_flattens_the_chat_only_for_an_external_runtime(
    app_session: AppSessionFactory, redis_url: str, in_process: bool
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u1, run1 = await _send(db, session, "one")
        await _reply(db, run1, "answer one")
        _u2, run2 = await _send(db, session, "two")
        flattened = await flattened_task_text(db, run2)
        own = str(run2.context["task"])
        task_id = session.task_id
        await _commit(db, tenant)
    assert task_id is not None
    runtime: _Recording | _InProcessRecording = (
        _InProcessRecording(task_id) if in_process else _Recording(task_id)
    )
    await execute_run(
        RunMessage(run_id=str(run2.id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=runtime,
    )
    assert flattened == "User: one\nYou (assistant): answer one\nUser: two"
    assert runtime.task_text == (own if in_process else flattened)
