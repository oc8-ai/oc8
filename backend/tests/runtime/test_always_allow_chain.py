"""The whole chain of an always-allowed write in a chat, through the real
executor and engine: the run parks on the person's own write card, their
rule decides it at the park (`auto_decide_by_rules`, not stubbed), the
resumed leg runs the call with the conversation and finishes, and a message
queued with "send after" during the run becomes the next turn. The audit
log holds the call's allow and its result exactly once.

Only the model (a scripted stream) and the tool server (a recorder) are
fakes; every other link is the production path."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.chat.turn_context import turn_keys
from oc8.modelrouter import ToolCall
from oc8.runtime.adapter import Oc8AgentRuntime
from oc8.runtime.executor import execute_run
from oc8.runtime.queue import RunMessage
from oc8.runtime.run_inputs import CARRIED_OVER_STEP, add_run_input
from oc8.runtime.states import RunState
from tests.agent.test_engine_direct_access import _copilot, _patch
from tests.agent.test_engine_parallel_reads import _RecordingServer, _turn
from tests.approvals.test_tool_allow_rules import _rule
from tests.conftest import AppSessionFactory
from tests.copilot.direct_access_helpers import chat_run, direct_office, grant

pytestmark = pytest.mark.asyncio

QUEUED = "Danach bitte die Liste."
EARLIER = "XYZ steht noch auf offen."
TURN = "Setz Datensatz 7 bitte auf erledigt."


class _WritesDuringTheRun(_RecordingServer):
    """The tool server; while the first leg reads, the person queues a
    message with "send after" -- through its own session, as the API does."""

    def __init__(self, app_session: AppSessionFactory) -> None:
        super().__init__(delay_s=0)
        self.app_session = app_session
        self.tenant: uuid.UUID | None = None
        self.run_id: uuid.UUID | None = None
        self.member_id: uuid.UUID | None = None

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "search_records" and self.tenant is not None:
            async with self.app_session(self.tenant) as w:
                run = await w.get(m.AgentRun, self.run_id)
                member = await w.get(m.OrgMember, self.member_id)
                assert run is not None and member is not None
                await add_run_input(w, run=run, member=member, text=QUEUED, delivery="next_turn")
                await w.commit()
        return await super().call(name, arguments)


async def _execute(tenant: uuid.UUID, run_id: uuid.UUID) -> None:
    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=Oc8AgentRuntime(),
    )


async def test_a_rule_answered_write_resumes_finishes_and_carries_the_queued_turn(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _WritesDuringTheRun(app_session)
    _opened, _bus, stream = _patch(
        monkeypatch,
        recorder,
        [
            # Leg 1: a read, then the write that parks on Anna's card.
            _turn("", ToolCall(id="c1", name="search_records", arguments={"q": "XYZ"})),
            _turn("", ToolCall(id="c2", name="update_record", arguments={"id": 7})),
            # Leg 2, after her rule approved the card: the call again, now
            # pre-decided, then the answer.
            _turn("", ToolCall(id="c3", name="update_record", arguments={"id": 7})),
            _turn("Erledigt."),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await grant(db, office)
        await _copilot(db, office)
        rule = await _rule(db, office)
        # A real chat turn: an earlier exchange, then the person's new message.
        parent: m.ChatMessage | None = None
        for role, content in (("user", "Was ist mit XYZ?"), ("assistant", EARLIER), ("user", TURN)):
            message = m.ChatMessage(
                tenant_id=tenant,
                session_id=office.session_id,
                role=role,
                content=content,
                parent_id=parent.id if parent is not None else None,
            )
            db.add(message)
            await db.flush()
            transcript_until = parent
            parent = message
        assert parent is not None and transcript_until is not None
        session = await db.get(m.ChatSession, office.session_id)
        assert session is not None
        session.current_leaf_id = parent.id
        run = await chat_run(
            db,
            office,
            state=RunState.QUEUED.value,
            extra={
                **turn_keys(
                    turn_role="user",
                    message=TURN,
                    turn_text=TURN,
                    transcript_until=transcript_until.id,
                ),
                "reply_to_message_id": str(parent.id),
                # What the web send records for the sender's live token.
                "operator_role": "member",
                "acting_token_role": "member",
            },
        )
        run_id, turn_id = run.id, parent.id
        await db.commit()
    recorder.tenant, recorder.run_id, recorder.member_id = tenant, run_id, office.member_id

    # Leg 1 parks; the executor's park path lets the rule decide the card.
    await _execute(tenant, run_id)
    async with app_session(tenant) as db:
        card = (
            await db.execute(
                select(m.ApprovalRequest).where(
                    m.ApprovalRequest.action_type == "tool_send",
                    m.ApprovalRequest.payload["run_id"].astext == str(run_id),
                )
            )
        ).scalar_one()
        assert card.status == "approved"
        assert card.reason == f"always-allow rule {rule.id}"
        parked = await db.get(m.AgentRun, run_id)
        assert parked is not None
        assert parked.state == RunState.QUEUED.value, "the rule's approval re-queued the run"
        waiting = (
            (await db.execute(select(m.RunInput).where(m.RunInput.run_id == run_id)))
            .scalars()
            .all()
        )
        assert [(i.delivery, i.consumed_at) for i in waiting] == [("next_turn", None)]
    assert [name for name, _s, _e in recorder.windows] == ["search_records"]

    # Leg 2: the approved write runs, with the conversation, and the run ends.
    await _execute(tenant, run_id)
    assert [name for name, _s, _e in recorder.windows] == ["search_records", "update_record"]
    # The resumed leg's first completion: the conversation, the turn and the
    # first leg's own work, not a bare resume instruction.
    resumed = stream.sent[2]
    assert EARLIER in resumed and TURN in resumed
    assert "search_records {" in resumed and "→ ok" in resumed, "the first leg's work digest"

    async with app_session(tenant) as db:
        done = await db.get(m.AgentRun, run_id)
        assert done is not None
        assert done.state == RunState.DONE.value
        [queued] = (
            (await db.execute(select(m.RunInput).where(m.RunInput.run_id == run_id)))
            .scalars()
            .all()
        )
        assert queued.consumed_step == CARRIED_OVER_STEP
        messages = list(
            (
                await db.execute(
                    select(m.ChatMessage)
                    .where(m.ChatMessage.session_id == office.session_id)
                    .order_by(m.ChatMessage.created_at, m.ChatMessage.id)
                )
            ).scalars()
        )
        assert [(msg.role, msg.content) for msg in messages[-2:]] == [
            ("assistant", "Erledigt."),
            ("user", QUEUED),
        ]
        assert messages[-2].reply_to_id == turn_id
        followups = list(
            (
                await db.execute(
                    select(m.AgentRun).where(
                        m.AgentRun.context["chat_session_id"].astext == str(office.session_id),
                        m.AgentRun.state == RunState.QUEUED.value,
                    )
                )
            ).scalars()
        )
        assert len(followups) == 1, "carried over as exactly one next turn"
        assert followups[0].context["reply_to_message_id"] == str(messages[-1].id)

        events = list(
            (
                await db.execute(
                    select(m.AuditEvent)
                    .where(m.AuditEvent.tenant_id == tenant)
                    .order_by(m.AuditEvent.seq)
                )
            ).scalars()
        )
    calls = [e for e in events if e.action == "tool.call:update_record"]
    results = [e for e in events if e.action == "tool.result:update_record"]
    # One result: the write ran once, on the resumed leg.
    assert len(results) == 1
    assert results[0].resource["outcome"] == "ok"
    # Two tool.call lines, both "allow": `tool.call` records the PEP's
    # decision, and the risk gate that parked the first leg's call on Anna's
    # card answers after it (engine.py, `_gate` "ask") -- the hold itself
    # shows as the card and its always-allow approval in between.
    assert [c.decision for c in calls] == ["allow", "allow"]
    [used] = [e for e in events if e.action == "approval.allow_rule_used"]
    assert calls[0].seq < used.seq < calls[1].seq < results[0].seq
