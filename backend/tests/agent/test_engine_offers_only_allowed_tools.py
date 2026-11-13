"""The in-process engine offers a connection's tool only when the gate would
let it through (authz.pdp.offers_tool) -- the frame's `only`, the agent's
narrowing, the right the tool needs. Approval-gated tools stay offered."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from tests.agent.test_engine_parallel_reads import _fixture, _RecordingServer, _StubSession, _turn
from tests.conftest import AppSessionFactory

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.modelrouter import CompletionResult, NeutralTool, ToolCall, chunk_from_result

pytestmark = pytest.mark.asyncio

SERVER_TOOLS = ["files_list", "files_upload", "mail_search", "mail_send"]
SCOPES = {"read": ["files_list", "mail_search"], "modify": ["files_upload", "mail_send"]}


class _OfferRecorder:
    def __init__(self, script: list[CompletionResult]) -> None:
        self.script = script
        self.offered: list[set[str]] = []

    def __call__(self, *args: Any, **kw: Any) -> Any:
        self.offered.append({t.name for t in kw.get("tools") or []})

        async def _gen() -> Any:
            yield chunk_from_result(self.script.pop(0))

        return _gen()


async def _run(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: dict[str, Any],
    narrowing: dict[str, Any] | None = None,
    script: list[CompletionResult] | None = None,
) -> tuple[set[str], _RecordingServer, Any]:
    recorder = _RecordingServer(delay_s=0)

    async def _open(*_a: Any, **_kw: Any) -> _StubSession:
        return _StubSession(recorder, SERVER_TOOLS)

    stream = _OfferRecorder(script or [_turn("fertig")])
    monkeypatch.setattr("oc8.agent.engine._open_mcp_session", _open)
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", stream)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, conn = await _fixture(db, tenant, tool_scopes=SCOPES)
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = {"tools": {"things": policy}}
        agent.narrowing = narrowing or {}
        await db.flush()
        result = await run_agent(db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn)
    return stream.offered[0] & set(SERVER_TOOLS), recorder, result


async def test_the_frames_only_withholds_mail(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    offered, _, _ = await _run(
        app_session,
        monkeypatch,
        policy={
            "enabled": True,
            "read": True,
            "modify": True,
            "only": ["files_list", "files_upload"],
        },
    )
    assert offered == {"files_list", "files_upload"}


async def test_modify_off_withholds_writing_tools(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    offered, _, _ = await _run(
        app_session, monkeypatch, policy={"enabled": True, "read": True, "modify": False}
    )
    assert offered == {"files_list", "mail_search"}


async def test_the_agents_narrowing_narrows_further(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    offered, _, _ = await _run(
        app_session,
        monkeypatch,
        policy={
            "enabled": True,
            "read": True,
            "modify": True,
            "only": ["files_list", "files_upload"],
        },
        narrowing={"tools": {"things": {"enabled": True, "read": True, "only": ["files_list"]}}},
    )
    assert offered == {"files_list"}


async def test_a_switched_off_connection_offers_nothing(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    offered, _, _ = await _run(
        app_session, monkeypatch, policy={"enabled": False, "read": True, "modify": True}
    )
    assert offered == set()


async def test_an_approval_gated_tool_stays_offered(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    offered, _, _ = await _run(
        app_session,
        monkeypatch,
        policy={
            "enabled": True,
            "read": True,
            "modify": True,
            "only": ["files_upload"],
            "approval_actions": ["files_upload"],
        },
    )
    assert offered == {"files_upload"}


async def test_a_hidden_tool_named_anyway_is_still_denied(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    offered, recorder, result = await _run(
        app_session,
        monkeypatch,
        policy={"enabled": True, "read": True, "modify": True, "only": ["files_list"]},
        script=[
            _turn("", ToolCall(id="c1", name="mail_search", arguments={})),
            _turn("fertig"),
        ],
    )
    assert "mail_search" not in offered
    assert recorder.windows == []
    entry = result.tool_calls[-1]
    assert entry["state"] == "denied", result.tool_calls
    assert entry["reason"] == "'things' does not grant the tool 'mail_search' in this frame"


async def test_find_tools_never_returns_a_withheld_tool(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enough tools to defer most of them behind find_tools: the deferred
    catalog is built from the offer, so a withheld tool is not in it."""
    files = [f"files_op_{i}" for i in range(40)]
    mail = [f"mail_op_{i}" for i in range(40)]
    recorder = _RecordingServer(delay_s=0)

    async def _open(*_a: Any, **_kw: Any) -> _StubSession:
        return _StubSession(recorder, files + mail)

    stream = _OfferRecorder(
        [
            _turn(
                "",
                ToolCall(id="f1", name="find_tools", arguments={"query": "mail op"}),
                ToolCall(id="f2", name="find_tools", arguments={"query": "files op"}),
            ),
            _turn("fertig"),
        ]
    )
    monkeypatch.setattr("oc8.agent.engine._open_mcp_session", _open)
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", stream)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, conn = await _fixture(db, tenant, tool_scopes={"read": files + mail, "modify": []})
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = {"tools": {"things": {"enabled": True, "read": True, "only": files}}}
        await db.flush()
        result = await run_agent(db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn)

    assert "find_tools" in stream.offered[0], "the list must be large enough to defer"
    assert not any(name.startswith("mail_") for name in stream.offered[0])
    found = [str(e.get("result", "")) for e in result.tool_calls if e.get("tool") == "find_tools"]
    assert found, result.tool_calls
    assert all("mail_op" not in text for text in found), found
    assert any("files_op" in text for text in found), found


class _CodingToolset:
    def __init__(self) -> None:
        self.tools = [
            NeutralTool(name=n, description=n, parameters={"type": "object", "properties": {}})
            for n in ("fs_read", "fs_write", "shell_run")
        ]
        self.called: list[str] = []

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        self.called.append(name)
        return "ok"


async def _coding_run(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    policy: dict[str, Any],
    script: list[CompletionResult] | None = None,
) -> tuple[set[str], _CodingToolset, Any]:
    toolset = _CodingToolset()
    stream = _OfferRecorder(script or [_turn("fertig")])
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", stream)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _conn = await _fixture(db, tenant, tool_scopes=SCOPES)
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        dept.frame = {"tools": {"coding": policy}}
        await db.flush()
        result = await run_agent(db, agent=agent, task_text="go", tenant_id=tenant, toolset=toolset)
    names = {t.name for t in toolset.tools}
    return stream.offered[0] & names, toolset, result


async def test_the_coding_toolset_follows_the_frames_only(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    offered, _, _ = await _coding_run(
        app_session,
        monkeypatch,
        {"enabled": True, "read": True, "modify": True, "only": ["fs_read", "fs_write"]},
    )
    assert offered == {"fs_read", "fs_write"}


async def test_a_read_only_coding_grant_offers_no_writing_tool(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    offered, _, _ = await _coding_run(
        app_session, monkeypatch, {"enabled": True, "read": True, "modify": False}
    )
    assert offered == {"fs_read"}


async def test_a_hidden_coding_tool_named_anyway_is_still_denied(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    offered, toolset, result = await _coding_run(
        app_session,
        monkeypatch,
        {"enabled": True, "read": True, "modify": True, "only": ["fs_read"]},
        script=[
            _turn("", ToolCall(id="c1", name="shell_run", arguments={"command": "ls"})),
            _turn("fertig"),
        ],
    )
    assert offered == {"fs_read"}
    assert toolset.called == []
    entry = result.tool_calls[-1]
    assert entry["state"] == "denied", result.tool_calls
    assert entry["reason"] == "'coding' does not grant the tool 'shell_run' in this frame"
