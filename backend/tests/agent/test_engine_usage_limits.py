"""The in-process engine enforces guardrail usage limits at dispatch.

Asked after every other gate, right before the call: a call the frame refuses
or holds for approval never counts, a call that failed is given back, and the
call over the limit is refused with a sentence the model can act on -- and is
seen by a person in the audit trail and the activity feed.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from tests.conftest import AppSessionFactory

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.modelrouter import CompletionResult, NeutralTool, ToolCall, Usage, chunk_from_result


def _turn(*calls: ToolCall) -> CompletionResult:
    return CompletionResult(
        text="",
        tool_calls=list(calls),
        usage=Usage(tokens_in=1, tokens_out=1),
        stop_reason="tool_use" if calls else "stop",
        provider="fake",
        model="fake",
    )


class _Script:
    def __init__(self, script: list[CompletionResult]) -> None:
        self.script = script

    def __call__(self, *args: Any, **kw: Any) -> Any:
        async def _gen() -> Any:
            yield chunk_from_result(self.script.pop(0))

        return _gen()


class _Server:
    def __init__(self, tools: list[str], failures: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.failures = failures or set()
        self.tools = [
            NeutralTool(name=n, description=n, parameters={"type": "object", "properties": {}})
            for n in tools
        ]

    async def __aenter__(self) -> _Server:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append(name)
        if name in self.failures:
            raise RuntimeError(f"{name} failed")
        return f"{name} ok"


async def _agent(
    db: Any,
    tenant: uuid.UUID,
    *,
    limits: list[dict[str, Any]],
    scopes: dict[str, list[str]],
    approval_actions: list[str] | None = None,
) -> tuple[m.Agent, m.McpConnection]:
    policy: dict[str, Any] = {"enabled": True, "read": True, "modify": True, "limits": limits}
    if approval_actions:
        policy["approval_actions"] = approval_actions
    dept = m.Department(tenant_id=tenant, name="Postfach", frame={"tools": {"email": policy}})
    db.add(dept)
    await db.flush()
    model_config = m.ModelConfig(
        tenant_id=tenant, provider="fake", model="fake", params={"parallel_tool_calls": True}
    )
    db.add(model_config)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        model_config_id=model_config.id,
        name="Mila",
        status="running",
        narrowing={},
        definition={},
        presentation={},
    )
    db.add(agent)
    await db.flush()
    conn = m.McpConnection(
        tenant_id=tenant,
        department_id=dept.id,
        name="email",
        transport="stdio",
        server_url="stdio://email",
        connected=True,
        config={"command": "x", "args": []},
        scopes=scopes,
    )
    db.add(conn)
    await db.flush()
    return agent, conn


def _setup(monkeypatch: pytest.MonkeyPatch, server: _Server, turns: list[CompletionResult]) -> None:
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", lambda *_a, **_k: server)
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", _Script(turns))


SEND_ONCE = [{"tools": ["mail_send", "mail_reply"], "max": 1, "per": "day"}]
SCOPES = {"read": ["mail_get"], "modify": ["mail_send", "mail_reply"]}


async def test_the_call_over_the_limit_is_refused_audited_and_shown(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    server = _Server(["mail_get", "mail_send", "mail_reply"])
    _setup(
        monkeypatch,
        server,
        [
            _turn(ToolCall(id="c1", name="mail_send", arguments={"to": "a@example.org"})),
            _turn(ToolCall(id="c2", name="mail_reply", arguments={"uid": "7"})),
            _turn(),
        ],
    )
    async with app_session(tenant) as db:
        agent, conn = await _agent(db, tenant, limits=SEND_ONCE, scopes=SCOPES)
        result = await run_agent(db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn)
        agent_id = agent.id

    assert server.calls == ["mail_send"]
    refused = result.tool_calls[1]
    assert refused["tool"] == "mail_reply"
    assert "Daily limit of 1 reached for mail_send/mail_reply; resets at 00:00 UTC" in str(
        refused["result"]
    )
    async with app_session(tenant) as db:
        denied = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.action == "tool.call:mail_reply",
                        m.AuditEvent.decision == "deny",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [e.reason for e in denied] == [
            "Daily limit of 1 reached for mail_send/mail_reply; resets at 00:00 UTC."
        ]
        shown = (
            (
                await db.execute(
                    select(m.ActivityEvent).where(
                        m.ActivityEvent.agent_id == agent_id, m.ActivityEvent.status == "warning"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any("Daily limit of 1" in e.message for e in shown)


class _Answers(_Server):
    """A server whose calls reach the tool and then go wrong in a given way."""

    def __init__(self, tools: list[str], outcome: dict[str, BaseException | str]) -> None:
        super().__init__(tools)
        self.outcome = outcome

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append(name)
        got = self.outcome.get(name)
        if isinstance(got, BaseException):
            raise got
        return got if got is not None else f"{name} ok"


@pytest.mark.parametrize(
    "outcome",
    [
        TimeoutError("the SMTP server did not answer in time"),
        RuntimeError("connection reset after DATA"),
        "ERROR: 550 mailbox unavailable",
    ],
    ids=["timeout", "exception", "error-result"],
)
async def test_a_call_that_reached_the_tool_keeps_its_count_even_when_it_failed(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    outcome: BaseException | str,
) -> None:
    """A timed-out or failed send may still have gone out: the tool was
    reached, so the count stays. Giving it back would let a flaky server send
    more than the limit allows."""
    tenant = uuid.uuid4()
    server = _Answers(["mail_send", "mail_reply"], {"mail_send": outcome})
    _setup(
        monkeypatch,
        server,
        [
            _turn(ToolCall(id="c1", name="mail_send", arguments={"to": "a@example.org"})),
            _turn(ToolCall(id="c2", name="mail_reply", arguments={"uid": "7"})),
            _turn(),
        ],
    )
    async with app_session(tenant) as db:
        agent, conn = await _agent(db, tenant, limits=SEND_ONCE, scopes=SCOPES)
        result = await run_agent(db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn)

    assert server.calls == ["mail_send"]
    assert "Daily limit of 1 reached" in str(result.tool_calls[1]["result"])


async def test_an_identical_retry_at_the_limit_gets_its_first_result_back(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replayed write is not a new call: it is answered before the count
    is asked, so a retry at 1/1 still gets the first result, not a refusal."""
    tenant = uuid.uuid4()
    server = _Server(["mail_send"])
    same = {"to": "a@example.org", "subject": "Hi"}
    _setup(
        monkeypatch,
        server,
        [
            _turn(ToolCall(id="c1", name="mail_send", arguments=same)),
            _turn(ToolCall(id="c2", name="mail_send", arguments=same)),
            _turn(),
        ],
    )
    async with app_session(tenant) as db:
        agent, conn = await _agent(db, tenant, limits=SEND_ONCE, scopes=SCOPES)
        result = await run_agent(db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn)

    assert server.calls == ["mail_send"]
    assert "limit" not in str(result.tool_calls[1]["result"]).lower()
    assert "mail_send ok" in str(result.tool_calls[1]["result"])


async def test_a_held_call_whose_limit_is_full_is_refused_not_parked(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking a person to approve a call the limit will refuse anyway wastes
    their time: a full limit refuses before the approval is raised."""
    tenant = uuid.uuid4()
    server = _Server(["mail_send"])
    _setup(
        monkeypatch,
        server,
        [_turn(ToolCall(id="c1", name="mail_send", arguments={"to": "a@example.org"})), _turn()],
    )
    async with app_session(tenant) as db:
        agent, conn = await _agent(
            db,
            tenant,
            limits=[{"tools": ["mail_send"], "max": 0}],
            scopes=SCOPES,
            approval_actions=["modify"],
        )
        result = await run_agent(db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn)
        approvals = (
            (
                await db.execute(
                    select(m.ApprovalRequest).where(m.ApprovalRequest.tenant_id == tenant)
                )
            )
            .scalars()
            .all()
        )

    assert result.status != "waiting_for_approval"
    assert approvals == []
    assert server.calls == []
    assert "Daily limit of 0 reached for mail_send" in str(result.tool_calls[0]["result"])


async def test_a_limited_read_is_never_prefetched_in_a_parallel_batch(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parallel read pre-pass runs calls before the per-call gate. A
    counted read must break that batch, or both calls would run before the
    count could refuse the second."""
    tenant = uuid.uuid4()
    server = _Server(["mail_get"])
    _setup(
        monkeypatch,
        server,
        [
            _turn(
                ToolCall(id="c1", name="mail_get", arguments={"uid": "1"}),
                ToolCall(id="c2", name="mail_get", arguments={"uid": "2"}),
            ),
            _turn(),
        ],
    )
    async with app_session(tenant) as db:
        agent, conn = await _agent(
            db, tenant, limits=[{"tools": ["mail_get"], "max": 1}], scopes=SCOPES
        )
        result = await run_agent(db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn)

    assert server.calls == ["mail_get"]
    assert "Daily limit of 1 reached for mail_get" in str(result.tool_calls[1]["result"])


async def test_a_stored_malformed_limit_denies_its_tool_and_the_run_goes_on(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    server = _Server(["mail_send"])
    _setup(
        monkeypatch,
        server,
        [_turn(ToolCall(id="c1", name="mail_send", arguments={"to": "a@example.org"})), _turn()],
    )
    async with app_session(tenant) as db:
        agent, conn = await _agent(db, tenant, limits=[{"max": 5}], scopes=SCOPES)
        result = await run_agent(db, agent=agent, task_text="go", tenant_id=tenant, mcp_conn=conn)

    assert result.status == "done"
    assert server.calls == []
