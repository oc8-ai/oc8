"""resolve_tool_approval records the operator's verdict on the run and re-queues
it; the resume instruction + pre-decided map steer/execute exactly the decided
calls."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.engine import RunResult
from oc8.agent.harness.caps import ModelCaps
from oc8.agent.preamble import build_run_preamble
from oc8.chat.digest import build_work_digest
from oc8.chat.replay import chat_replay_of
from oc8.copilot.direct_access import direct_access_resume_note
from oc8.runtime.adapter import Oc8AgentRuntime
from oc8.runtime.approval_resume import (
    call_signature,
    pre_decided_map,
    rejection_text,
    resolve_tool_approval,
    resolved_entries,
    resume_instruction,
)
from oc8.runtime.executor import execute_run
from oc8.runtime.queue import RunMessage
from oc8.runtime.repository import RunRepository
from oc8.runtime.run_context import merge_context
from oc8.runtime.states import RunState
from tests.agents.test_preamble import _fixed_retrieval
from tests.chat.test_message_tree import _commit, _reply, _send, _session
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _suspended_run(session: AppSessionFactory, tenant: uuid.UUID):
    async with session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="D", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="A",
            status="waiting_for_approval",
            narrowing={},
            definition={},
        )
        db.add(agent)
        await db.flush()
        task = m.Task(
            tenant_id=tenant, department_id=dept.id, title="t", state="waiting_for_approval"
        )
        db.add(task)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent.id,
            task_id=task.id,
            state=RunState.WAITING_FOR_APPROVAL.value,
            context={"task": "original"},
        )
        db.add(run)
        await db.flush()
        return run.id, task.id


async def test_approve_records_verdict_and_requeues(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    run_id, task_id = await _suspended_run(app_session, tenant)
    args = {"model": "sale.order", "values": {"x": 1}}
    async with app_session(tenant) as db:
        ar = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=task_id,
            action_type="tool_send",
            status="approved",
            payload={"tool": "create_record", "arguments": args},
        )
        db.add(ar)
        await db.flush()
        run = await resolve_tool_approval(db, approval=ar, decision="approve")
        assert run is not None and run.id == run_id
        assert run.state == RunState.QUEUED.value
        entries = run.context["resolved_tool_approvals"]
        assert entries[0]["decision"] == "approve"
        assert entries[0]["sig"] == call_signature("create_record", args)


async def test_no_resume_when_run_not_waiting(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    _run_id, task_id = await _suspended_run(app_session, tenant)
    async with app_session(tenant) as db:
        run = (
            await db.execute(m.AgentRun.__table__.select().where(m.AgentRun.task_id == task_id))
        ).first()
        # Flip the run out of waiting so resume must decline.
        r = await db.get(m.AgentRun, run.id)
        r.state = RunState.DONE.value
        await db.flush()
        ar = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=task_id,
            action_type="tool_send",
            status="approved",
            payload={"tool": "create_record", "arguments": {}},
        )
        db.add(ar)
        await db.flush()
        assert await resolve_tool_approval(db, approval=ar, decision="approve") is None


async def _second_turn(
    session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A chat's two turns on ONE task: turn 1 finished, turn 2 parked on a
    held write. Returns (turn 1 run, turn 2 run, task)."""
    turn2_id, task_id = await _suspended_run(session, tenant)
    async with session(tenant) as db:
        turn2 = await db.get(m.AgentRun, turn2_id)
        assert turn2 is not None
        turn1 = m.AgentRun(
            tenant_id=tenant,
            agent_id=turn2.agent_id,
            task_id=task_id,
            state=RunState.DONE.value,
            context={"task": "turn 1"},
            created_at=turn2.created_at - dt.timedelta(minutes=1),
        )
        db.add(turn1)
        await db.flush()
        return turn1.id, turn2_id, task_id


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_a_second_turn_approval_resumes_the_run_that_raised_it(
    app_session: AppSessionFactory, decision: str
) -> None:
    """E2E F6: both turns of an agent chat share one task, so a lookup by task
    matched two runs (500 MultipleResultsFound) and the run parked forever."""
    tenant = uuid.uuid4()
    _turn1, turn2, task_id = await _second_turn(app_session, tenant)
    async with app_session(tenant) as db:
        ar = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=task_id,
            action_type="tool_send",
            status="pending",
            payload={"tool": "mail_send", "arguments": {"to": "x"}, "run_id": str(turn2)},
        )
        db.add(ar)
        await db.flush()
        run = await resolve_tool_approval(db, approval=ar, decision=decision)
        assert run is not None and run.id == turn2
        assert run.state == RunState.QUEUED.value
        assert run.context["resolved_tool_approvals"][0]["decision"] == decision


async def test_an_approval_without_run_id_resumes_the_task_run_that_raised_it(
    app_session: AppSessionFactory,
) -> None:
    """A card raised before its raiser named the run (still pending across the
    upgrade): the run that raised it is the task's latest run started before
    it, never "any run of the task"."""
    tenant = uuid.uuid4()
    _turn1, turn2, task_id = await _second_turn(app_session, tenant)
    async with app_session(tenant) as db:
        ar = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=task_id,
            action_type="tool_send",
            status="pending",
            payload={"tool": "mail_send", "arguments": {}},
        )
        db.add(ar)
        await db.flush()
        run = await resolve_tool_approval(db, approval=ar, decision="approve")
        assert run is not None and run.id == turn2


def test_resume_instruction_lists_approved_and_rejected() -> None:
    entries = [
        {"sig": "a", "tool": "create_record", "arguments": {"v": 1}, "decision": "approve"},
        {"sig": "b", "tool": "update_record", "arguments": {"v": 2}, "decision": "reject"},
    ]
    text = resume_instruction(entries)
    assert "APPROVED" in text and "create_record" in text
    assert "REJECTED" in text and "update_record" in text
    assert pre_decided_map(entries) == {"a": "approve", "b": "reject"}
    assert resolved_entries(entries) == {"a": entries[0], "b": entries[1]}


async def test_resume_instruction_is_english_and_carries_the_reason() -> None:
    entries = [
        {"sig": "a", "tool": "create_record", "arguments": {"v": 1}, "decision": "approve"},
        {
            "sig": "b",
            "tool": "update_record",
            "arguments": {"v": 2},
            "decision": "reject",
            "reason": "wrong customer",
            "decided_by_name": "Ada Admin",
        },
        {"sig": "c", "tool": "delete_record", "arguments": {"v": 3}, "decision": "reject"},
    ]
    assert resume_instruction(entries) == "\n".join(
        [
            "Resuming after a decision. A person decided one or more actions. Do NOT "
            "repeat earlier steps (no new searches, no new notes).",
            'APPROVED — carry out exactly this action now: create_record with {"v": 1}',
            'REJECTED — do NOT carry out this action: update_record with {"v": 2} — rejected '
            "by Ada Admin: wrong customer. Adjust your approach instead of retrying the same "
            "call.",
            'REJECTED — do NOT carry out this action: delete_record with {"v": 3}',
            "Then sum up in 1-2 sentences and call no further tools.",
        ]
    )
    assert rejection_text(entries[2]) == "operator rejected this action"
    # A reason without a name the model may be told.
    assert rejection_text({**entries[1], "decided_by_name": ""}) == (
        "rejected by a reviewer: wrong customer. "
        "Adjust your approach instead of retrying the same call."
    )


async def _decider(db: Any, tenant: uuid.UUID, name: str) -> m.OrgMember:
    member = m.OrgMember(
        tenant_id=tenant, subject=f"s-{uuid.uuid4()}", subject_uuid=uuid.uuid4(), display_name=name
    )
    db.add(member)
    await db.flush()
    return member


@pytest.mark.parametrize(
    ("display_name", "told"), [("Ada Admin", "Ada Admin"), ("ada@example.com", "")]
)
async def test_reason_is_capped_at_500_chars_on_one_line(
    app_session: AppSessionFactory, display_name: str, told: str
) -> None:
    tenant = uuid.uuid4()
    _run_id, task_id = await _suspended_run(app_session, tenant)
    async with app_session(tenant) as db:
        decider = await _decider(db, tenant, display_name)
        ar = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=task_id,
            action_type="tool_send",
            status="rejected",
            payload={"tool": "update_record", "arguments": {}},
            reason="wrong\ncustomer\t" + "x" * 600,
            decided_by=decider.id,
        )
        db.add(ar)
        await db.flush()
        run = await resolve_tool_approval(db, approval=ar, decision="reject")
        assert run is not None
        (entry,) = run.context["resolved_tool_approvals"]
    assert entry["reason"].startswith("wrong customer xxx")
    assert len(entry["reason"]) == 500 and "\n" not in entry["reason"]
    # Never an email address: the model is told a neutral word instead.
    assert entry["decided_by_name"] == told
    assert rejection_text(entry).startswith(f"rejected by {told or 'a reviewer'}: wrong customer")


async def test_an_approval_without_reason_or_decider_records_empty_strings(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    _run_id, task_id = await _suspended_run(app_session, tenant)
    async with app_session(tenant) as db:
        ar = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=task_id,
            action_type="tool_send",
            status="rejected",
            payload={"tool": "update_record", "arguments": {}},
        )
        db.add(ar)
        await db.flush()
        run = await resolve_tool_approval(db, approval=ar, decision="reject")
        assert run is not None
        (entry,) = run.context["resolved_tool_approvals"]
    assert (entry["reason"], entry["decided_by_name"]) == ("", "")
    assert rejection_text(entry) == "operator rejected this action"


class _InProcess(Oc8AgentRuntime):
    """The in-process engine's door, recording what the executor hands it."""

    def __init__(self, task_id: uuid.UUID | None) -> None:
        self.task_id = task_id
        self.seen: dict[str, Any] = {}

    async def execute(self, db: Any, **kw: Any) -> RunResult:  # type: ignore[override]
        self.seen = kw
        return RunResult(
            task_id=self.task_id or uuid.uuid4(),
            agent_id=kw["agent"].id,
            status="done",
            output="ok",
        )


#: What the run did before it parked: a read, then the held write.
_CALLS = [
    {"tool": "search_records", "arguments": {"q": "ACME"}, "state": "done", "result": "3 rows"},
    {"tool": "update_record", "arguments": {"id": 7}, "state": "awaiting_approval"},
]


async def _parked_chat_leg(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, woken_by: str
) -> tuple[uuid.UUID, uuid.UUID | None, str]:
    """A chat's second turn, parked on a held write after a read, then woken
    by `woken_by`; returns the run, its task and what the leg must be told."""
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u1, run1 = await _send(db, session, "one")
        await _reply(db, run1, "answer one")
        _u2, run = await _send(db, session, "two")
        run.state = RunState.WAITING_FOR_APPROVAL.value
        await merge_context(db, run, {"toolCalls": _CALLS})
        if woken_by == "approval":
            ada = await _decider(db, tenant, "Ada Admin")
            ar = m.ApprovalRequest(
                tenant_id=tenant,
                agent_id=session.agent_id,
                task_id=run.task_id,
                action_type="tool_send",
                status="rejected",
                payload={"tool": "update_record", "arguments": {"id": 7}, "run_id": str(run.id)},
                reason="wrong customer",
                decided_by=ada.id,
            )
            db.add(ar)
            await db.flush()
            assert await resolve_tool_approval(db, approval=ar, decision="reject") is not None
            told = resume_instruction(run.context["resolved_tool_approvals"])
            assert "rejected by Ada Admin: wrong customer." in told
        else:
            access = {"tool_key": "things", "label": "Things", "decision": "approve"}
            await merge_context(db, run, {"resolved_direct_access": access})
            await RunRepository(db).transition(run, RunState.QUEUED)
            told = direct_access_resume_note(access)
        run_id, task_id = run.id, session.task_id
        await _commit(db, tenant)
    return run_id, task_id, told


async def _resumed_preamble(
    app_session: AppSessionFactory, tenant: uuid.UUID, run_id: uuid.UUID, seen: dict[str, Any]
) -> list[Any]:
    """The preamble the in-process engine builds from what it was handed."""
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        agent = await db.get(m.Agent, run.agent_id)
        assert agent is not None
        pre = await build_run_preamble(
            db,
            agent=agent,
            tenant_id=tenant,
            task_text=seen["task_text"],
            frame={},
            model_locality="cloud",
            caps=ModelCaps(),
            max_steps=40,
            run_id=run.id,
            replay=chat_replay_of(run.context),
            resume=seen["resume"],
        )
    return pre.messages


@pytest.mark.parametrize("woken_by", ["approval", "direct_access"])
async def test_a_resumed_chat_leg_keeps_the_conversation(
    app_session: AppSessionFactory,
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
    woken_by: str,
) -> None:
    """The leg after a decision still has the conversation and the person's
    new turn; what the run did before the pause and the decision follow
    them, after the cached prefix."""
    _fixed_retrieval(monkeypatch, [])
    tenant = uuid.uuid4()
    run_id, task_id, told = await _parked_chat_leg(app_session, tenant, woken_by=woken_by)
    runtime = _InProcess(task_id)
    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=runtime,
    )
    assert runtime.seen["task_text"] == "User: two", "the turn is kept, not replaced"
    messages = await _resumed_preamble(app_session, tenant, run_id, runtime.seen)

    turns = [msg for msg in messages if msg.role != "system"]
    assert [msg.role for msg in turns] == ["user", "assistant", "user", "assistant", "user"]
    assert turns[0].content == "one"
    assert str(turns[1].content).startswith("answer one")
    assert str(turns[2].content).endswith("\n\ntwo")
    assert turns[3].content == build_work_digest(_CALLS, run_id=run_id, restricted=False)
    assert turns[4].content == told
    marked = [i for i, msg in enumerate(messages) if msg.cache_breakpoint]
    assert marked[-1] == len(messages) - 4, "the replayed turns stay the last marked prefix"


async def test_a_resumed_chat_leg_without_tool_calls_says_so(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixed_retrieval(monkeypatch, [])
    tenant = uuid.uuid4()
    run_id, task_id, _told = await _parked_chat_leg(app_session, tenant, woken_by="direct_access")
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        await merge_context(db, run, {"toolCalls": []})
        await _commit(db, tenant)
    runtime = _InProcess(task_id)
    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=runtime,
    )
    assert runtime.seen["resume"].own_work_digest is None
    messages = await _resumed_preamble(app_session, tenant, run_id, runtime.seen)
    assert messages[-2].role == "assistant"
    assert messages[-2].content == "[no tool calls yet]"


async def test_a_failing_digest_never_fails_the_resumed_leg(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("digest unreadable")

    monkeypatch.setattr("oc8.runtime.executor.build_work_digest", _boom)
    tenant = uuid.uuid4()
    run_id, task_id, told = await _parked_chat_leg(app_session, tenant, woken_by="approval")
    runtime = _InProcess(task_id)
    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=runtime,
    )
    resume = runtime.seen["resume"]
    assert (resume.own_work_digest, resume.instruction) == (None, told)


async def test_a_non_chat_resume_keeps_todays_shape(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    """A trigger run has no conversation to keep: the resume instruction is
    still its whole task, now in English, and no resume is handed on."""
    tenant = uuid.uuid4()
    run_id, task_id = await _suspended_run(app_session, tenant)
    async with app_session(tenant) as db:
        ar = m.ApprovalRequest(
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=task_id,
            action_type="tool_send",
            status="approved",
            payload={"tool": "create_record", "arguments": {"v": 1}},
        )
        db.add(ar)
        await db.flush()
        run = await resolve_tool_approval(db, approval=ar, decision="approve")
        assert run is not None
        entries = run.context["resolved_tool_approvals"]
        await _commit(db, tenant)
    runtime = _InProcess(task_id)
    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=runtime,
    )
    assert runtime.seen["task_text"] == resume_instruction(entries)
    assert runtime.seen["task_text"].startswith("Resuming after a decision.")
    assert "resume" not in runtime.seen
