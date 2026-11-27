"""A chat replayed to the in-process engine as native turns (spec §5.1): the
path to the turn's `transcript_until`, mapped message by message and merged
where two neighbours share a role."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.agent.harness.caps import ModelCaps
from oc8.agent.preamble import build_run_preamble
from oc8.chat.replay import (
    RESTRICTED_PLACEHOLDER,
    ChatReplay,
    chat_replay_of,
    replay_messages,
)
from oc8.chat.service import regenerate_reply
from oc8.chat.turn_context import NOTICE_PREFIX, flattened_task_text
from oc8.modelrouter import CompletionResult, Usage, chunk_from_result
from tests.chat.test_message_tree import _commit, _reply, _send, _session
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _add(
    session: m.ChatSession, role: str, content: str, parent: m.ChatMessage | None, **kw: Any
) -> m.ChatMessage:
    return m.ChatMessage(
        tenant_id=session.tenant_id,
        session_id=session.id,
        role=role,
        content=content,
        parent_id=parent.id if parent is not None else None,
        **kw,
    )


async def _chain(
    db: Any, session: m.ChatSession, rows: list[tuple[str, str, dict[str, Any]]]
) -> list[m.ChatMessage]:
    """`rows` as one path, each the child of the one before; the last is the leaf."""
    made: list[m.ChatMessage] = []
    for role, content, kw in rows:
        message = _add(session, role, content, made[-1] if made else None, **kw)
        db.add(message)
        await db.flush()
        made.append(message)
    session.current_leaf_id = made[-1].id
    await db.flush()
    return made


async def test_user_and_assistant_turns_alternate_natively(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        for n in range(3):
            _u, run = await _send(db, session, f"question {n}")
            await _reply(db, run, f"answer {n}")
        _u, run = await _send(db, session, "question 3")
        replay = chat_replay_of(run.context)
        assert replay is not None
        turns = await replay_messages(db, replay=replay, model_locality="cloud")
    assert [t.role for t in turns] == ["user", "assistant"] * 3
    assert [t.content for t in turns] == [
        "question 0",
        "answer 0",
        "question 1",
        "answer 1",
        "question 2",
        "answer 2",
    ]
    assert not any(str(t.content).startswith("User:") for t in turns)


async def test_assistant_turn_carries_its_digest_block(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    digest = 'lookup {"id": "42"} → ok: found'
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u, _a = await _chain(
            db,
            session,
            [("user", "find 42", {}), ("assistant", "Found it.", {"work_digest": digest})],
        )
        turns = await replay_messages(
            db, replay=ChatReplay(session.id, _a.id), model_locality="cloud"
        )
    assert turns[-1].role == "assistant"
    assert turns[-1].content == "Found it." + "\n\n[Work done in this turn]\n" + digest


async def test_followup_and_notice_keep_their_speaker_prefix(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    notice = NOTICE_PREFIX + "the agent was refused"
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        rows = await _chain(
            db,
            session,
            [
                ("user", "hi", {}),
                ("assistant", "hello", {}),
                ("followup", "check the invoice", {}),
                ("assistant", "checked", {}),
                ("followup", notice, {}),
            ],
        )
        turns = await replay_messages(
            db, replay=ChatReplay(session.id, rows[-1].id), model_locality="cloud"
        )
    assert turns[2].role == "user"
    assert turns[2].content == (
        "Follow-up (scheduled by you, not typed by the user): check the invoice"
    )
    assert turns[4].role == "user"
    assert turns[4].content == f"oc8 (a notice from the system, not typed by the user): {notice}"


async def test_same_role_messages_are_merged(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        rows = await _chain(
            db,
            session,
            [("user", "first", {}), ("user", "second", {}), ("assistant", "both seen", {})],
        )
        turns = await replay_messages(
            db, replay=ChatReplay(session.id, rows[-1].id), model_locality="cloud"
        )
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[0].content == "first\n\nsecond"


async def test_assistant_without_text_says_the_run_state(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        failed = m.AgentRun(
            tenant_id=tenant, agent_id=session.agent_id, source="chat", state="failed", context={}
        )
        db.add(failed)
        await db.flush()
        rows = await _chain(
            db, session, [("user", "do it", {}), ("assistant", "", {"run_id": failed.id})]
        )
        turns = await replay_messages(
            db, replay=ChatReplay(session.id, rows[-1].id), model_locality="cloud"
        )
    assert turns[-1].content == "[no answer — run ended: failed]"


async def test_earlier_images_become_names(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _no_bytes(key: str) -> bytes:
        raise AssertionError(f"image bytes loaded for {key}")

    monkeypatch.setattr("oc8.storage.s3.get_object", _no_bytes)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        rows = await _chain(
            db, session, [("user", "what is this?", {}), ("assistant", "a chart", {})]
        )
        db.add(
            m.FileAttachment(
                tenant_id=tenant,
                owner_type="chat_message",
                owner_id=rows[0].id,
                bucket_key=f"t/{uuid.uuid4()}",
                filename="chart.png",
                content_type="image/png",
                size_bytes=10,
                is_image=True,
            )
        )
        await db.flush()
        turns = await replay_messages(
            db, replay=ChatReplay(session.id, rows[-1].id), model_locality="cloud"
        )
    assert turns[0].content == "what is this?\n[image: chart.png]"
    assert isinstance(turns[0].content, str)


async def test_a_restricted_turn_is_withheld_after_switching_to_a_cloud_model(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        rows = await _chain(
            db,
            session,
            [
                ("user", "salary of Anna?", {}),
                (
                    "assistant",
                    "Anna earns 5000.",
                    {"restricted": True, "work_digest": "hr_lookup → ok"},
                ),
            ],
        )
        replay = ChatReplay(session.id, rows[-1].id)
        cloud = await replay_messages(db, replay=replay, model_locality="cloud")
        local = await replay_messages(db, replay=replay, model_locality="local")
    assert cloud[-1].content == RESTRICTED_PLACEHOLDER
    assert local[-1].content == "Anna earns 5000.\n\n[Work done in this turn]\nhr_lookup → ok"


async def test_regenerate_replays_the_path_to_its_parent_not_the_leaf(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u1, run1 = await _send(db, session, "one")
        r1 = await _reply(db, run1, "answer one")
        u2, run2 = await _send(db, session, "BRANCH-A")
        await _reply(db, run2, "ANSWER-A")
        # Branch B: an edit of "BRANCH-A", now the branch on screen.
        edited = _add(session, "user", "BRANCH-B", r1)
        db.add(edited)
        await db.flush()
        session.current_leaf_id = edited.id
        await _commit(db, tenant)
        run_regen = await regenerate_reply(
            db,
            session=session,
            tenant_id=tenant,
            user_message=u2,
            originating_operator="op",
            operator_role=None,
        )
        replay = chat_replay_of(run_regen.context)
        assert replay is not None
        turns = await replay_messages(db, replay=replay, model_locality="cloud")
    joined = "\n".join(str(t.content) for t in turns)
    assert "answer one" in joined
    assert "BRANCH-B" not in joined


async def test_replay_failure_falls_back_to_flattened_text(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _broken(*_a: Any, **_kw: Any) -> list[Any]:
        raise RuntimeError("replay broke")

    monkeypatch.setattr("oc8.agent.preamble.replay_messages", _broken)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u, run1 = await _send(db, session, "earlier question")
        await _reply(db, run1, "earlier answer")
        _u, run = await _send(db, session, "new question")
        agent = await db.get(m.Agent, session.agent_id)
        expected = await flattened_task_text(db, run)
        pre = await build_run_preamble(
            db,
            agent=agent,
            tenant_id=tenant,
            task_text=str(run.context["task"]),
            frame={},
            model_locality="cloud",
            caps=ModelCaps(),
            max_steps=40,
            run_id=run.id,
            replay=chat_replay_of(run.context),
        )
    assert "earlier answer" in expected
    assert pre.messages[-1].role == "user"
    assert pre.messages[-1].content == expected
    assert not any(msg.role == "assistant" for msg in pre.messages)


class _RecordingRouter:
    """Answers at once and keeps the messages of the first request."""

    def __init__(self) -> None:
        self.messages: list[Any] | None = None

    async def complete(self, req: Any) -> CompletionResult:
        if self.messages is None:
            self.messages = list(req.messages)
        return CompletionResult(
            text="done",
            tool_calls=[],
            usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop",
            provider="fake",
            model="fake",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def test_the_engine_replays_a_chat_turn(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _RecordingRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        _u, run1 = await _send(db, session, "earlier question")
        await _reply(db, run1, "earlier answer")
        _u, run = await _send(db, session, "new question")
        agent = await db.get(m.Agent, session.agent_id)
        await run_agent(
            db, agent=agent, task_text=str(run.context["task"]), tenant_id=tenant, run_id=run.id
        )
    assert router.messages is not None
    turns = [msg for msg in router.messages if msg.role != "system"]
    assert [(msg.role, msg.content) for msg in turns[:2]] == [
        ("user", "earlier question"),
        ("assistant", "earlier answer"),
    ]
    assert turns[2].role == "user"
    assert str(turns[2].content).startswith("# Run context")
    assert str(turns[2].content).endswith("\n\nnew question")
