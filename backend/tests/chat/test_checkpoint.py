"""Chat checkpoints (spec §5.3): a long chat is summarised in the background,
a replay starts from the newest checkpoint on the person's path, and a run
start never replays more than `SYNC_RATIO` of the window -- a checkpoint made
on the spot, else the newest messages that fit."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.agent.harness.caps import ModelCaps
from oc8.agent.harness.prompts import compaction_instruction
from oc8.agent.preamble import build_run_preamble
from oc8.chat import checkpoint
from oc8.chat.replay import RESTRICTED_PLACEHOLDER, ChatReplay, replay_messages
from oc8.chat.service import create_session
from oc8.config import get_settings
from oc8.modelrouter.types import CompletionResult, NeutralMessage, Usage
from tests.chat.test_message_tree import _reply, _send, _session
from tests.chat.test_replay import _chain, _RecordingRouter
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

HEADINGS = (
    "Goal of the conversation",
    "Decisions made",
    "Facts, files and objects referenced (with ids)",
    "Open questions",
    "Current work state",
    "Next move",
)


class _FakeComplete:
    """Stands in for `complete_with_fallback`: records each call's messages
    and answers with `answer`, or raises it."""

    def __init__(self, answer: str | BaseException = "SUMMARY") -> None:
        self.answer = answer
        self.calls: list[list[NeutralMessage]] = []

    async def __call__(self, *_a: Any, **kw: Any) -> CompletionResult:
        self.calls.append(list(kw["messages"]))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return CompletionResult(
            text=self.answer,
            tool_calls=[],
            usage=Usage(tokens_in=10, tokens_out=5),
            stop_reason="stop",
            provider="fake",
            model="fake-model",
        )


class _Jobs:
    """Collects spawned background jobs instead of running them."""

    def __init__(self) -> None:
        self.jobs: list[Callable[[], Awaitable[None]]] = []

    def __call__(self, job: Callable[[], Awaitable[None]]) -> None:
        self.jobs.append(job)


def _alternating(n: int, chars: int) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        ("user" if i % 2 == 0 else "assistant", f"m{i:02d}" + "x" * (chars - 3), {})
        for i in range(n)
    ]


def _replayed(messages: list[NeutralMessage]) -> list[NeutralMessage]:
    """The preamble's replayed turns: after the system messages, before the
    final user message."""
    return [msg for msg in messages if msg.role != "system"][:-1]


async def _preamble(db: Any, session: m.ChatSession, leaf: m.ChatMessage, window: int) -> Any:
    agent = await db.get(m.Agent, session.agent_id)
    return await build_run_preamble(
        db,
        agent=agent,
        tenant_id=session.tenant_id,
        task_text="User: new question",
        frame={},
        model_locality="cloud",
        caps=ModelCaps(context_window_tokens=window),
        max_steps=40,
        replay=ChatReplay(session.id, leaf.id),
    )


async def _another_session(db: Any, session: m.ChatSession) -> m.ChatSession:
    """A second chat with the same agent, for a new member of the tenant."""
    member = m.OrgMember(
        tenant_id=session.tenant_id,
        all_departments=True,
        subject=f"op-{uuid.uuid4().hex[:8]}",
        subject_uuid=uuid.uuid4(),
    )
    db.add(member)
    await db.flush()
    return await create_session(
        db, tenant_id=session.tenant_id, agent_id=session.agent_id, member_id=member.id
    )


def _checkpoint_row(
    session: m.ChatSession, upto: m.ChatMessage, summary: str, **kw: Any
) -> m.ChatCheckpoint:
    return m.ChatCheckpoint(
        tenant_id=session.tenant_id,
        session_id=session.id,
        upto_message_id=upto.id,
        summary=summary,
        token_estimate=len(summary) // 4,
        model="fake-model",
        **kw,
    )


async def test_crossing_40_percent_schedules_a_background_checkpoint(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = _Jobs()
    monkeypatch.setattr("oc8.chat.checkpoint._spawn", jobs)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        # 1 000 tokens of window: 40 % is 1 600 chars.
        _u, run = await _send(db, session, "short question")
        run.context = {**run.context, "context_window_tokens": 1000}
        await _reply(db, run, "short answer")
        await asyncio.sleep(0)
        assert jobs.jobs == []

        _u, run = await _send(db, session, "q" * 1000)
        run.context = {**run.context, "context_window_tokens": 1000}
        await _reply(db, run, "a" * 1000)
        await asyncio.sleep(0)
    assert len(jobs.jobs) == 1


async def test_checkpoint_keeps_a_verbatim_tail(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.chat.checkpoint.complete_with_fallback", _FakeComplete())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        # 50 tokens each; 15 % of a 1 000-token window keeps three verbatim.
        rows = await _chain(db, session, _alternating(10, 200))
        made = await checkpoint.make_checkpoint(
            db, session=session, path=rows, window=1000, model_locality="local"
        )
        assert made is not None
        assert made.upto_message_id == rows[-4].id
        assert made.summary == "SUMMARY"

        # A newest message alone above the tail is still kept verbatim.
        session2 = await _another_session(db, session)
        rows2 = await _chain(db, session2, [*_alternating(4, 200), ("user", "y" * 2000, {})])
        made2 = await checkpoint.make_checkpoint(
            db, session=session2, path=rows2, window=1000, model_locality="local"
        )
        assert made2 is not None
        assert made2.upto_message_id == rows2[-2].id

        # Nothing to cut: the whole path fits the tail.
        session3 = await _another_session(db, session)
        rows3 = await _chain(db, session3, _alternating(2, 100))
        assert (
            await checkpoint.make_checkpoint(
                db, session=session3, path=rows3, window=1000, model_locality="local"
            )
            is None
        )


async def test_summary_prompt_follows_the_template(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = _FakeComplete()
    monkeypatch.setattr("oc8.chat.checkpoint.complete_with_fallback", complete)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        rows = await _chain(db, session, _alternating(10, 200))
        await checkpoint.make_checkpoint(
            db, session=session, path=rows, window=1000, model_locality="local"
        )
    assert len(complete.calls) == 1
    messages = complete.calls[0]
    last = str(messages[-1].content)
    assert messages[-1].role == "user"
    assert compaction_instruction() in last
    tail = last[last.index(compaction_instruction()) + len(compaction_instruction()) :]
    positions = [tail.index(heading) for heading in HEADINGS]
    assert positions == sorted(positions)
    assert last.endswith(HEADINGS[-1])
    # The summarised turns come first; the verbatim tail is not in them.
    joined = "\n".join(str(msg.content) for msg in messages)
    assert "m00" in joined and "m06" in joined
    assert "m07" not in joined


async def test_replay_starts_from_the_newest_checkpoint_on_the_path(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        rows = await _chain(db, session, _alternating(8, 50))
        db.add(_checkpoint_row(session, rows[1], "OLDER"))
        await db.flush()
        db.add(_checkpoint_row(session, rows[3], "NEWER"))
        await db.flush()
        turns = await replay_messages(
            db, replay=ChatReplay(session.id, rows[-1].id), model_locality="cloud"
        )
    assert turns[0].role == "user"
    assert turns[0].content == "[Conversation so far]\nNEWER\n\n" + rows[4].content
    assert [t.content for t in turns[1:]] == [r.content for r in rows[5:]]


async def test_a_checkpoint_on_another_branch_is_ignored(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        rows = await _chain(
            db,
            session,
            [("user", "q1", {}), ("assistant", "a1", {}), ("user", "BRANCH-A", {})],
        )
        branch_b = m.ChatMessage(
            tenant_id=tenant,
            session_id=session.id,
            role="user",
            content="BRANCH-B",
            parent_id=rows[1].id,
        )
        db.add(branch_b)
        await db.flush()
        db.add(_checkpoint_row(session, rows[2], "SUMMARY OF BRANCH A"))
        session.current_leaf_id = branch_b.id
        await db.flush()
        turns = await replay_messages(
            db, replay=ChatReplay(session.id, branch_b.id), model_locality="cloud"
        )
    joined = "\n".join(str(t.content) for t in turns)
    assert "SUMMARY OF BRANCH A" not in joined
    assert "BRANCH-A" not in joined
    assert [t.content for t in turns] == ["q1", "a1", "BRANCH-B"]


async def test_sync_checkpoint_when_over_70_percent_without_a_fresh_one(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("oc8.chat.checkpoint.complete_with_fallback", _FakeComplete("SYNC SUM"))
    real = checkpoint.make_checkpoint
    calls: list[Any] = []

    async def _spy(*a: Any, **kw: Any) -> Any:
        calls.append(kw)
        return await real(*a, **kw)

    monkeypatch.setattr("oc8.chat.checkpoint.make_checkpoint", _spy)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        # 10 x 100 tokens in a 1 000-token window: 100 %, no checkpoint yet.
        rows = await _chain(db, session, _alternating(10, 400))
        pre = await _preamble(db, session, rows[-1], window=1000)
        stored = (
            (
                await db.execute(
                    select(m.ChatCheckpoint).where(m.ChatCheckpoint.session_id == session.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(calls) == 1
    replayed = _replayed(pre.messages)
    assert str(replayed[0].content).startswith("[Conversation so far]\nSYNC SUM")
    assert checkpoint.estimate_tokens(replayed) <= 0.7 * 1000
    assert str(replayed[-1].content) == rows[-1].content
    assert len(stored) == 1 and stored[0].summary == "SYNC SUM"


async def test_a_background_checkpoint_spares_the_sync_one(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = _FakeComplete()
    monkeypatch.setattr("oc8.chat.checkpoint.complete_with_fallback", complete)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        rows = await _chain(db, session, _alternating(10, 400))
        # Written by the background job, as its cut would have it.
        db.add(_checkpoint_row(session, rows[8], "FRESH"))
        await db.flush()
        pre = await _preamble(db, session, rows[-1], window=1000)
    assert complete.calls == []
    replayed = _replayed(pre.messages)
    assert str(replayed[0].content).startswith("[Conversation so far]\nFRESH")


async def test_failed_summary_replays_the_most_recent_messages_that_fit(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "oc8.chat.checkpoint.complete_with_fallback", _FakeComplete(RuntimeError("down"))
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        rows = await _chain(db, session, _alternating(10, 400))
        pre = await _preamble(db, session, rows[-1], window=1000)
        stored = await db.scalar(
            select(m.ChatCheckpoint.id).where(m.ChatCheckpoint.session_id == session.id)
        )
    assert stored is None
    replayed = _replayed(pre.messages)
    joined = "\n".join(str(t.content) for t in replayed)
    assert checkpoint.estimate_tokens(replayed) <= 0.7 * 1000
    assert rows[-1].content in joined
    assert "m00" not in joined
    assert pre.messages[-1].role == "user"
    assert str(pre.messages[-1].content).endswith("new question")


async def test_one_oversized_message_still_starts_the_run(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = _FakeComplete()
    monkeypatch.setattr("oc8.chat.checkpoint.complete_with_fallback", complete)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        # 150 000 tokens: more than the whole 128k window on its own.
        (big,) = await _chain(db, session, [("user", "z" * 600_000, {})])
        pre = await _preamble(db, session, big, window=128_000)
    assert complete.calls == []
    replayed = _replayed(pre.messages)
    assert checkpoint.estimate_tokens(replayed) <= 0.7 * 128_000
    assert str(replayed[-1].content).startswith("zzz")
    assert str(replayed[-1].content).endswith("[… cut]")


async def test_a_cloud_checkpoint_never_sees_a_restricted_turn(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = _FakeComplete()
    monkeypatch.setattr("oc8.chat.checkpoint.complete_with_fallback", complete)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        agent = await db.get(m.Agent, session.agent_id)
        assert agent is not None
        agent.presentation = {"provider": "openai"}
        rows = await _chain(
            db,
            session,
            [
                ("user", "q" * 200, {}),
                ("assistant", "SECRET-XYZ " + "s" * 200, {"restricted": True}),
                *_alternating(8, 200),
            ],
        )
        # The run's model local, the session's cloud: still withheld.
        for locality in ("cloud", "local"):
            made = await checkpoint.make_checkpoint(
                db, session=session, path=rows, window=1000, model_locality=locality
            )
            assert made is not None and made.restricted is False
            await db.delete(made)
            await db.flush()
    assert len(complete.calls) == 2
    for messages in complete.calls:
        joined = "\n".join(str(msg.content) for msg in messages)
        assert "SECRET-XYZ" not in joined
        assert RESTRICTED_PLACEHOLDER in joined


async def test_a_restricted_checkpoint_is_never_replayed_to_a_cloud_model(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = _FakeComplete("LOCAL SUMMARY")
    monkeypatch.setattr("oc8.chat.checkpoint.complete_with_fallback", complete)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        # The default model is local: it may read the restricted answer.
        session = await _session(db, tenant)
        rows = await _chain(
            db,
            session,
            [
                ("user", "q" * 200, {}),
                ("assistant", "SECRET-XYZ " + "s" * 200, {"restricted": True}),
                *_alternating(8, 200),
            ],
        )
        made = await checkpoint.make_checkpoint(
            db, session=session, path=rows, window=1000, model_locality="local"
        )
        assert made is not None and made.restricted is True
        assert "SECRET-XYZ" in "\n".join(str(msg.content) for msg in complete.calls[0])
        local = await replay_messages(
            db, replay=ChatReplay(session.id, rows[-1].id), model_locality="local"
        )
        cloud = await replay_messages(
            db, replay=ChatReplay(session.id, rows[-1].id), model_locality="cloud"
        )
    assert "LOCAL SUMMARY" in str(local[0].content)
    assert not any("LOCAL SUMMARY" in str(t.content) for t in cloud)
    assert not any("SECRET-XYZ" in str(t.content) for t in cloud)


async def test_the_background_job_checkpoints_the_reply_path_once(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = _FakeComplete()
    monkeypatch.setattr("oc8.chat.checkpoint.complete_with_fallback", complete)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        for n in range(4):
            _u, run = await _send(db, session, f"q{n}" + "q" * 400)
            run.context = {**run.context, "context_window_tokens": 1000}
            reply = await _reply(db, run, f"a{n}" + "a" * 400)
        first = await checkpoint.checkpoint_after_reply(db, session_id=session.id, run_id=run.id)
        # A second job for the same reply finds the cut covered.
        second = await checkpoint.checkpoint_after_reply(db, session_id=session.id, run_id=run.id)
    assert first is not None and second is None
    assert first.upto_message_id != reply.id
    assert len(complete.calls) == 1


async def test_the_engine_reports_its_window_and_model(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _RecordingRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u, run = await _send(db, session, "question")
        agent = await db.get(m.Agent, session.agent_id)
        result = await run_agent(
            db, agent=agent, task_text=str(run.context["task"]), tenant_id=tenant, run_id=run.id
        )
    assert result.context_window_tokens == ModelCaps().context_window_tokens
    assert result.model == get_settings().default_model


async def test_checkpoints_are_tenant_isolated(app_session: AppSessionFactory) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    async with app_session(a) as db:
        session = m.ChatSession(tenant_id=a, agent_id=uuid.uuid4(), member_id=uuid.uuid4())
        db.add(session)
        await db.flush()
        db.add(
            m.ChatCheckpoint(
                tenant_id=a,
                session_id=session.id,
                upto_message_id=uuid.uuid4(),
                summary="mine",
                token_estimate=1,
                model="x",
            )
        )
        await db.flush()
    async with app_session(a) as db:
        mine = (await db.execute(m.ChatCheckpoint.__table__.select())).all()
        assert [row.summary for row in mine] == ["mine"]
    async with app_session(b) as db:
        rows = (await db.execute(m.ChatCheckpoint.__table__.select())).all()
        assert rows == []
