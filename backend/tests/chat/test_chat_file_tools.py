"""`search_chat_files` and `read_chat_file` (spec §5.4): a chat run searches
and pages through the large files of its OWN chat -- only the files on the
branch it answers, only for the chat's owner, and never another chat's."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.control_tools import ControlOutcome, execute_control_tool, offered_tools
from oc8.agent.harness.stages.b_risk_tier import CONTROL_TOOL_TIERS
from oc8.authz.pdp import Decision, Effect
from oc8.chat.files_kb import chat_has_files
from oc8.chat.modes import MODES, WRITING_CONTROL_TOOLS, mode_refusal
from oc8.knowledge.embedding_policy import LOCAL_AGENT_REASON
from oc8.modelrouter import ToolCall
from oc8.storage.attachments import INLINE_ATTACHMENT_CHARS
from tests.chat.test_chat_file_ingest import (
    _agent_on,
    _FakeEmbedRouter,
    _job_for,
    _message,
    _RecordingRouter,
    _upload,
)
from tests.chat.test_edit_and_switch import _answer, _client, _new_chat, _post, _seed_agent, _token
from tests.conftest import AppSessionFactory

TOOLS = ("search_chat_files", "read_chat_file")


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> _FakeEmbedRouter:
    fake = _FakeEmbedRouter()
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: fake)
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: fake)
    return fake


def _words(word: str, n: int) -> str:
    return ((word + " ") * (n // (len(word) + 1) + 1))[:n]


def _large(word: str) -> str:
    return _words(word, INLINE_ATTACHMENT_CHARS + 5_000)


async def _index(app_session: AppSessionFactory, tenant: uuid.UUID, aid: str) -> None:
    from oc8.knowledge.worker import ingest_job

    await ingest_job(_message(await _job_for(app_session, tenant, aid)))
    async with app_session(tenant) as db:
        row = await db.get(m.FileAttachment, uuid.UUID(aid))
        assert row is not None and row.index_status == "indexed"


async def _call(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    run_id: str,
    name: str,
    arguments: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> ControlOutcome:
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, uuid.UUID(run_id))
        assert run is not None
        if context is not None:
            run.context = context
            await db.flush()
        agent = await db.get(m.Agent, run.agent_id)
        task = await db.get(m.Task, run.task_id) if run.task_id is not None else None
        if task is None:
            task = m.Task(tenant_id=tenant, department_id=agent.department_id, title="t")
            db.add(task)
            await db.flush()
        out = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="1", name=name, arguments=arguments),
            decision=Decision(effect=Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=(run.context or {}).get("originating_operator"),
            run_id=run.id,
        )
    assert out is not None
    return out


async def _search(
    app_session: AppSessionFactory, tenant: uuid.UUID, run_id: str, query: str, **kw: Any
) -> str:
    return (
        await _call(app_session, tenant, run_id, "search_chat_files", {"query": query}, **kw)
    ).output


async def _read(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    run_id: str,
    file_id: str,
    page: int = 1,
    **kw: Any,
) -> str:
    args = {"file_id": file_id, "page": page}
    return (await _call(app_session, tenant, run_id, "read_chat_file", args, **kw)).output


def _agent() -> m.Agent:
    return m.Agent(tenant_id=uuid.uuid4(), department_id=uuid.uuid4(), name="A", is_team_lead=False)


def _names(**kw: Any) -> list[str]:
    return [
        t.name
        for t in offered_tools(_agent(), assigned_skills=[], active_skills=[], mcp_tools=[], **kw)
    ]


def test_both_tools_are_reads() -> None:
    for name in TOOLS:
        assert name not in WRITING_CONTROL_TOOLS
        assert CONTROL_TOOL_TIERS[name] == "read"
        assert mode_refusal(MODES["plan"], name, tool_scopes=None) is None
        assert mode_refusal(MODES["do"], name, tool_scopes=None) is None
        assert mode_refusal(MODES["ask"], name, tool_scopes=None) is not None


def test_tools_offered_only_when_the_chat_has_files() -> None:
    assert not set(TOOLS) & set(_names())
    assert set(TOOLS) <= set(_names(has_chat_files=True))
    assert set(TOOLS) <= set(_names(has_chat_files=True, chat_mode=MODES["plan"]))
    assert set(TOOLS) <= set(_names(has_chat_files=True, chat_mode=MODES["do"]))
    assert _names(has_chat_files=True, chat_mode=MODES["ask"]) == []


async def test_chat_has_files_only_with_a_live_chat_kb(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        small_sid = await _new_chat(c, tenant, agent_id)
        small = await _upload(c, tenant, small_sid, "small.txt", "just a few words")
        small_turn = await _post(c, tenant, small_sid, "see", attachmentIds=[small])
        large_sid = await _new_chat(c, tenant, agent_id)
        large = await _upload(c, tenant, large_sid, "large.txt", _large("alpha"))
        large_turn = await _post(c, tenant, large_sid, "see", attachmentIds=[large])
    async with app_session(tenant) as db:
        small_run = await db.get(m.AgentRun, uuid.UUID(small_turn["runId"]))
        large_run = await db.get(m.AgentRun, uuid.UUID(large_turn["runId"]))
        assert not await chat_has_files(db, small_run.context)
        assert await chat_has_files(db, large_run.context)
        assert not await chat_has_files(db, None)
        assert not await chat_has_files(db, {"chat_session_id": "not-a-uuid"})


async def test_search_returns_this_sessions_files_only(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        mine = await _new_chat(c, tenant, agent_id)
        mine_file = await _upload(c, tenant, mine, "mine.txt", _large("alpha"))
        other = await _new_chat(c, tenant, agent_id)
        other_file = await _upload(c, tenant, other, "other.txt", _large("zulu"))
        await _index(app_session, tenant, mine_file)
        await _index(app_session, tenant, other_file)
        await _post(c, tenant, other, "see", attachmentIds=[other_file])
        turn = await _post(c, tenant, mine, "see", attachmentIds=[mine_file])
    out = await _search(app_session, tenant, turn["runId"], "alpha")
    assert "alpha" in out
    assert f"file id {mine_file}" in out and "mine.txt" in out
    assert "zulu" not in out and other_file not in out


async def test_a_hidden_branch_does_not_leak(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    """The person edits their first turn: the second turn's file is on a branch
    the edited turn's run does not answer. It is neither found nor readable;
    the first turn's file (copied to the edit) still is."""
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        kept = await _upload(c, tenant, sid, "kept.txt", _large("alpha"))
        await _index(app_session, tenant, kept)
        first = await _post(c, tenant, sid, "one", attachmentIds=[kept])
        await _answer(app_session, tenant, first["runId"], "answer one")
        hidden = await _upload(c, tenant, sid, "hidden.txt", _large("zulu"))
        await _index(app_session, tenant, hidden)
        await _post(c, tenant, sid, "two", attachmentIds=[hidden])
        r = await c.post(
            f"/api/v1/chat/sessions/{sid}/messages/{first['id']}/edit",
            json={"content": "one, edited"},
            headers=_token(tenant),
        )
        assert r.status_code == 201, r.text
        edit_run = r.json()["runId"]

    out = await _search(app_session, tenant, edit_run, "zulu")
    assert "zulu" not in out and hidden not in out
    assert "alpha" in out and "kept.txt" in out
    refused = await _read(app_session, tenant, edit_run, hidden)
    assert "zulu" not in refused and refused.startswith("ERROR")


async def test_read_pages_through_the_whole_file(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    body = "HEAD-MARKER " + _words("alpha", 300_000) + " TAIL-MARKER"
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "book.txt", body)
        await _index(app_session, tenant, aid)
        turn = await _post(c, tenant, sid, "summarise", attachmentIds=[aid])
    first = await _read(app_session, tenant, turn["runId"], aid)
    assert "HEAD-MARKER" in first and "TAIL-MARKER" not in first
    pages = -(-len(body) // 8000)
    assert f"page 1/{pages}" in first
    assert "book.txt" in first
    last = await _read(app_session, tenant, turn["runId"], aid, page=pages)
    assert f"page {pages}/{pages}" in last
    assert "TAIL-MARKER" in last, "beyond the 200,000 characters the turn could carry"


async def test_a_file_of_another_session_is_refused(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        mine = await _new_chat(c, tenant, agent_id)
        mine_file = await _upload(c, tenant, mine, "mine.txt", _large("alpha"))
        other = await _new_chat(c, tenant, agent_id)
        other_file = await _upload(c, tenant, other, "other.txt", _large("zulu"))
        await _post(c, tenant, other, "see", attachmentIds=[other_file])
        turn = await _post(c, tenant, mine, "see", attachmentIds=[mine_file])
    refused = await _read(app_session, tenant, turn["runId"], other_file)
    assert refused.startswith("ERROR") and "zulu" not in refused
    unknown = await _read(app_session, tenant, turn["runId"], str(uuid.uuid4()))
    assert unknown == refused, "the same answer: no oracle for another chat's ids"
    assert (await _read(app_session, tenant, turn["runId"], "nonsense")).startswith("ERROR")


async def test_tools_report_still_indexing(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "report.txt", _large("alpha"))
        turn = await _post(c, tenant, sid, "summarise", attachmentIds=[aid])
    out = await _search(app_session, tenant, turn["runId"], "alpha")
    assert "still indexing" in out
    assert "read_chat_file" in out and aid in out
    page = await _read(app_session, tenant, turn["runId"], aid)
    assert "alpha" in page and "page 1/" in page


async def test_only_the_chat_owners_own_turn_may_use_them(
    app_session: AppSessionFactory, redis_url: str, minio_url: str, router: _FakeEmbedRouter
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "report.txt", _large("alpha"))
        await _index(app_session, tenant, aid)
        turn = await _post(c, tenant, sid, "see", attachmentIds=[aid])
    run_id = turn["runId"]
    async with app_session(tenant) as db:
        sent = dict((await db.get(m.AgentRun, uuid.UUID(run_id))).context)
    for change in (
        {"originating_operator": "mallory"},
        {"originating_operator": None},
        {"chat_session_id": str(uuid.uuid4())},
    ):
        context = {**sent, **change}
        out = await _search(app_session, tenant, run_id, "alpha", context=context)
        assert out.startswith("ERROR") and "alpha" not in out, change
        out = await _read(app_session, tenant, run_id, aid, context=context)
        assert out.startswith("ERROR") and "alpha" not in out, change
    # A messenger turn is its owner's too (Ruling 58): nobody acts, the owner
    # is the person the turn is for.
    messenger = {
        **sent,
        "originating_operator": None,
        "for_person": "op",
        "chat_channel": "telegram",
        "chat_channel_external_id": "1",
    }
    assert "alpha" in await _search(app_session, tenant, run_id, "alpha", context=messenger)
    stranger = {**messenger, "for_person": "mallory"}
    assert (await _search(app_session, tenant, run_id, "alpha", context=stranger)).startswith(
        "ERROR"
    )


async def test_a_local_agents_question_stays_local(
    app_session: AppSessionFactory,
    redis_url: str,
    minio_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = _RecordingRouter()
    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: recording)
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: recording)
    tenant = uuid.uuid4()
    agent_id = await _agent_on(app_session, tenant, "ollama")
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        aid = await _upload(c, tenant, sid, "notes.txt", _large("alpha"))
        await _index(app_session, tenant, aid)
        turn = await _post(c, tenant, sid, "see", attachmentIds=[aid])
    recording.require_local.clear()
    out = await _search(app_session, tenant, turn["runId"], "alpha")
    assert "alpha" in out
    assert recording.require_local == [LOCAL_AGENT_REASON]
