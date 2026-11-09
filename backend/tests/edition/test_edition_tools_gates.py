"""Spec §8 invariant 10: an edition tool passes the same gates as every other
tool -- offer, mode, PEP, a call-time entitlement check, audit -- in every
runtime (in-process engine, isolated control plane, MCP gateway)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from tests.conftest import AppSessionFactory

from oc8 import models as m
from oc8.agent.control_tools import (
    EDITION_TOOL_FAILED,
    ControlOutcome,
    execute_control_tool,
    offered_tools,
)
from oc8.agent.harness.stages.b_authorize import authorize
from oc8.authz.pdp import Decision, Effect
from oc8.chat.modes import MODES, mode_refusal
from oc8.chat.service import record_assistant_reply
from oc8.edition.runtime import RuntimeComposition
from oc8.edition.tools import (
    MAX_PARTS,
    EditionOffer,
    EditionTool,
    EditionToolContext,
    edition_offer,
    use_edition_tools,
)
from oc8.licensing import CommunityEntitlements
from oc8.modelrouter import CompletionResult, NeutralTool, ToolCall, Usage, chunk_from_result
from oc8.runtime.supervision_hook import NOOP_SUPERVISION_RUN_HOOK

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True)
class _Granted:
    def has(self, capability: str) -> bool:
        return True

    def limit(self, name: str) -> int | None:
        return None


async def _granted(_t: uuid.UUID) -> _Granted:
    return _Granted()


async def _none(_t: uuid.UUID) -> CommunityEntitlements:
    return CommunityEntitlements()


#: Every executor run, by tool name -- "the executor never ran" is checked here.
RAN: list[str] = []


async def _write_doc(ctx: EditionToolContext) -> ControlOutcome:
    RAN.append("create_document")
    return ControlOutcome(output="Document created.", parts=[{"type": "document_ref", "id": "d1"}])


async def _read_doc(ctx: EditionToolContext) -> ControlOutcome:
    RAN.append("read_document")
    return ControlOutcome(output="The document says hello.")


async def _explodes(ctx: EditionToolContext) -> ControlOutcome:
    RAN.append("explode")
    raise RuntimeError("password=hunter2 leaked in the message")


async def _many_parts(ctx: EditionToolContext) -> ControlOutcome:
    RAN.append("many_parts")
    return ControlOutcome(output="ok", parts=[{"type": "chip", "n": i} for i in range(15)])


def _tool(name: str) -> NeutralTool:
    return NeutralTool(name=name, description=name, parameters={"type": "object"})


WRITE = EditionTool(
    tool=_tool("create_document"), execute=_write_doc, capability="enterprise.canvas", writes=True
)
READ = EditionTool(
    tool=_tool("read_document"), execute=_read_doc, capability="enterprise.canvas", writes=False
)
EXPLODE = EditionTool(
    tool=_tool("explode"), execute=_explodes, capability="enterprise.canvas", writes=False
)
MANY = EditionTool(
    tool=_tool("many_parts"), execute=_many_parts, capability="enterprise.canvas", writes=False
)
ALL = [WRITE, READ, EXPLODE, MANY]


@pytest.fixture(autouse=True)
def _reset_ran() -> None:
    RAN.clear()


def _authorize(agent: m.Agent, name: str, offer: EditionOffer, **kw: Any) -> Decision:
    return authorize(
        agent,
        ToolCall(id="1", name=name, arguments={}),
        frame={},
        tool_policies={},
        connection_key=kw.pop("connection_key", None),
        tool_scopes=kw.pop("tool_scopes", None),
        edition_offer=offer,
        **kw,
    )


# ----------------------------------------------------------- offer and PEP


async def test_modes_withhold_a_writing_edition_tool() -> None:
    agent = m.Agent(tenant_id=uuid.uuid4(), department_id=uuid.uuid4(), name="A")
    with use_edition_tools(ALL, _granted):
        plan = MODES["plan"]
        assert mode_refusal(plan, "create_document", tool_scopes=None) is not None
        assert mode_refusal(plan, "read_document", tool_scopes=None) is None

        offer = await edition_offer(agent.tenant_id, door="web", chat_mode=plan)
        assert "create_document" not in offer.names
        assert "read_document" in offer.names
        names = {
            t.name
            for t in offered_tools(
                agent,
                assigned_skills=[],
                active_skills=[],
                mcp_tools=[],
                chat_mode=plan,
                # Even handed the writing tool, the mode filter drops it.
                edition_tools=[WRITE.tool, READ.tool],
            )
        }
        assert "create_document" not in names
        assert "read_document" in names

        # Called anyway in /plan: the PEP refuses it with the mode's reason.
        full = await edition_offer(agent.tenant_id, door="web")
        denied = _authorize(agent, "create_document", full, chat_mode=plan)
        assert denied.effect is Effect.DENY and "/plan" in (denied.reason or "")
        assert _authorize(agent, "create_document", full).effect is Effect.ALLOW

        ask = await edition_offer(agent.tenant_id, door="web", chat_mode=MODES["ask"])
        assert ask.tools == ()


async def test_the_pep_refuses_an_unoffered_or_unentitled_edition_tool() -> None:
    agent = m.Agent(tenant_id=uuid.uuid4(), department_id=uuid.uuid4(), name="A")
    with use_edition_tools(ALL, _none):
        offer = await edition_offer(agent.tenant_id, door="web")
        assert offer.tools == ()
        decision = _authorize(agent, "create_document", offer)
        assert decision.effect is Effect.DENY
        assert "unknown tool" in (decision.reason or "")
    with use_edition_tools(ALL, _granted):
        # Registered and entitled, but this caller passed no offer at all.
        assert _authorize(agent, "read_document", EditionOffer()).effect is Effect.DENY


async def test_an_agent_tool_of_the_same_name_is_never_the_edition_tools() -> None:
    agent = m.Agent(tenant_id=uuid.uuid4(), department_id=uuid.uuid4(), name="A")
    with use_edition_tools(ALL, _granted):
        offer = await edition_offer(agent.tenant_id, door="web", taken={"read_document"})
        assert "read_document" not in offer.names
        assert not offer.claims("read_document")
        # The agent's own (unclassified, so writing) connection tool: the edition
        # tool's read classification must not let it through /plan.
        decision = _authorize(
            agent,
            "read_document",
            offer,
            chat_mode=MODES["plan"],
            tool_scopes={"read": []},
            connection_key="odoo",
        )
        assert decision.effect is Effect.DENY and "/plan" in (decision.reason or "")


# ----------------------------------------------------------- the dispatcher


async def _world(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.Task, m.AgentRun, m.ChatSession]:
    dept = m.Department(tenant_id=tenant, name="Ops", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
    db.add(agent)
    await db.flush()
    session = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=uuid.uuid4())
    task = m.Task(tenant_id=tenant, department_id=dept.id, title="t")
    db.add_all([session, task])
    await db.flush()
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=agent.id,
        source="chat",
        state="running",
        context={"chat_session_id": str(session.id)},
    )
    db.add(run)
    await db.flush()
    return agent, task, run, session


async def _call(
    db: Any,
    tenant: uuid.UUID,
    agent: m.Agent,
    task: m.Task,
    run: m.AgentRun,
    name: str,
    offer: EditionOffer,
    decision: Decision | None = None,
    arguments: dict[str, Any] | None = None,
) -> ControlOutcome | None:
    return await execute_control_tool(
        db,
        tenant_id=tenant,
        agent=agent,
        task=task,
        tc=ToolCall(id="1", name=name, arguments=arguments or {}),
        decision=decision or Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
        run_id=run.id,
        edition_offer=offer,
    )


async def _audit(db: Any) -> list[m.AuditEvent]:
    return list(
        (
            await db.execute(
                select(m.AuditEvent)
                .where(m.AuditEvent.action == "edition_tool.called")
                .order_by(m.AuditEvent.seq)
            )
        )
        .scalars()
        .all()
    )


async def test_unentitled_call_is_refused_and_audited(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run, _ = await _world(db, tenant)
        with use_edition_tools([WRITE], _granted):
            stale_offer = await edition_offer(tenant, door="web")
        # Entitlement revoked after the offer was made: checked again at call time.
        with use_edition_tools([WRITE], _none):
            out = await _call(db, tenant, agent, task, run, "create_document", stale_offer)
        audit = await _audit(db)
    assert out is not None and out.output.startswith("ERROR") and "unknown tool" in out.output
    assert RAN == []
    assert len(audit) == 1 and audit[0].resource["allowed"] is False
    assert audit[0].resource["capability"] == "enterprise.canvas"


async def test_an_unoffered_call_is_refused_and_never_runs(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run, _ = await _world(db, tenant)
        with use_edition_tools([WRITE, READ], _granted):
            # Offered on this run: only the read tool (as in /plan).
            offer = await edition_offer(tenant, door="web", chat_mode=MODES["plan"])
            out = await _call(db, tenant, agent, task, run, "create_document", offer)
            # And a mode DENY is honoured even for an offered tool.
            denied = await _call(
                db,
                tenant,
                agent,
                task,
                run,
                "read_document",
                offer,
                decision=Decision(Effect.DENY, "/plan changes nothing"),
            )
        audit = await _audit(db)
    assert out is not None and "unknown tool" in out.output
    assert denied is not None and denied.output == "ERROR: /plan changes nothing"
    assert RAN == []
    assert [a.resource["allowed"] for a in audit] == [False, False]


async def test_entitled_call_runs_and_its_parts_reach_the_message(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run, session = await _world(db, tenant)
        with use_edition_tools([WRITE], _granted):
            offer = await edition_offer(tenant, door="web")
            out = await _call(
                db,
                tenant,
                agent,
                task,
                run,
                "create_document",
                offer,
                arguments={"title": "secret-argument"},
            )
        audit = await _audit(db)
        run.state = "done"
        await record_assistant_reply(db, run=run, output="Here it is.")
        msg = (
            await db.execute(select(m.ChatMessage).where(m.ChatMessage.session_id == session.id))
        ).scalar_one()
    assert out is not None and out.output == "Document created."
    assert out.parts is None, "the dispatcher stored the parts; nobody stores them twice"
    assert msg.parts == [{"type": "document_ref", "id": "d1"}]
    assert RAN == ["create_document"]
    assert len(audit) == 1
    assert audit[0].decision == "allow"
    assert audit[0].resource == {
        "tool": "create_document",
        "capability": "enterprise.canvas",
        "run_id": str(run.id),
        "allowed": True,
    }
    assert "secret-argument" not in str(audit[0].resource)


async def test_a_failing_executor_is_contained(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run, _session = await _world(db, tenant)
        with use_edition_tools([EXPLODE, READ], _granted):
            offer = await edition_offer(tenant, door="web")
            out = await _call(db, tenant, agent, task, run, "explode", offer)
            # The run goes on: the session still works for the next call.
            after = await _call(db, tenant, agent, task, run, "read_document", offer)
        audit = await _audit(db)
    assert out is not None and out.output == EDITION_TOOL_FAILED
    assert "hunter2" not in out.output
    assert after is not None and after.output == "The document says hello."
    failed = audit[0]
    assert failed.resource["allowed"] is True
    assert failed.resource["error"] == "RuntimeError"
    assert "hunter2" not in str(failed.resource) and "hunter2" not in (failed.reason or "")


async def test_a_tenant_mismatch_is_refused(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run, _ = await _world(db, tenant)
        foreign = m.Task(tenant_id=uuid.uuid4(), department_id=task.department_id, title="x")
        with use_edition_tools([READ], _granted):
            offer = await edition_offer(tenant, door="web")
            out = await execute_control_tool(
                db,
                tenant_id=tenant,
                agent=agent,
                task=foreign,
                tc=ToolCall(id="1", name="read_document", arguments={}),
                decision=Decision(Effect.ALLOW),
                assigned_skills=[],
                active_skills=[],
                mcp_conn=None,
                originating_operator=None,
                run_id=run.id,
                edition_offer=offer,
            )
        audit = await _audit(db)
    assert out is not None and "unknown tool" in out.output
    assert RAN == []
    assert audit[0].resource["allowed"] is False


async def test_parts_stay_bounded_across_calls(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task, run, _ = await _world(db, tenant)
        with use_edition_tools([MANY], _granted):
            offer = await edition_offer(tenant, door="web")
            await _call(db, tenant, agent, task, run, "many_parts", offer)
            await _call(db, tenant, agent, task, run, "many_parts", offer)
        await db.refresh(run)
    assert RAN == ["many_parts", "many_parts"]
    assert len(run.context["parts"]) == MAX_PARTS


# ------------------------------------------------------- in-process engine


class _CallsThenStops:
    """Calls one tool, then answers -- recording what it was offered and told."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.requests: list[Any] = []

    async def complete(self, req: Any) -> CompletionResult:
        self.requests.append(req)
        if len(self.requests) == 1:
            return CompletionResult(
                text="",
                tool_calls=[ToolCall(id="c1", name=self.name, arguments={"q": "secret-q"})],
                usage=Usage(1, 1),
                stop_reason="tool_use",
                provider="ollama",
                model="m",
            )
        return CompletionResult(
            text="done",
            tool_calls=[],
            usage=Usage(1, 1),
            stop_reason="stop",
            provider="ollama",
            model="m",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


def _tool_result_text(router: _CallsThenStops) -> str:
    second = router.requests[1]
    return str(next(msg.content for msg in second.messages if msg.role == "tool"))


async def _engine_run(
    app_session: AppSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    entitlements: Any,
) -> _CallsThenStops:
    from oc8.agent.engine import run_agent

    router = _CallsThenStops(name)
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Rep")
        db.add(agent)
        await db.flush()
        with use_edition_tools([READ], entitlements):
            await run_agent(db, agent=agent, task_text="look it up", tenant_id=tenant)
        events = (
            (await db.execute(select(m.AuditEvent).where(m.AuditEvent.tenant_id == tenant)))
            .scalars()
            .all()
        )
    # Every call audited, and no argument of it anywhere in the audit.
    assert [e.action for e in events].count("edition_tool.called") == 1
    assert not any("secret-q" in str(e.resource) for e in events)
    return router


async def test_the_engine_offers_and_runs_an_entitled_edition_tool(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = await _engine_run(app_session, monkeypatch, "read_document", _granted)
    assert "read_document" in {t.name for t in router.requests[0].tools}
    assert RAN == ["read_document"]
    assert _tool_result_text(router).endswith("The document says hello.")


async def test_the_engine_refuses_an_unentitled_edition_tool(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = await _engine_run(app_session, monkeypatch, "read_document", _none)
    assert "read_document" not in {t.name for t in router.requests[0].tools}
    assert RAN == []
    assert "unknown tool" in _tool_result_text(router)


# ------------------------------------------------------------- MCP gateway


def _composition(entitlements: Any) -> RuntimeComposition:
    return RuntimeComposition(
        NOOP_SUPERVISION_RUN_HOOK,
        edition_tools=[READ, WRITE, _same_name_as_mcp()],
        tenant_entitlements=entitlements,
    )


async def _shadow(ctx: EditionToolContext) -> ControlOutcome:
    RAN.append("search_records")
    return ControlOutcome(output="edition answered")


def _same_name_as_mcp() -> EditionTool:
    return EditionTool(
        tool=_tool("search_records"), execute=_shadow, capability="enterprise.canvas", writes=False
    )


async def _gateway_rpc(
    composition: RuntimeComposition, token: str, method: str, params: dict[str, Any] | None = None
) -> Any:
    from tests.api.test_mcp_gateway import MCP

    from oc8.main import create_app

    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    app = create_app(runtime_composition=composition)
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(MCP, json=body, headers={"Authorization": f"Bearer {token}"})
            return r.json()


async def test_the_gateway_offers_gates_and_audits_edition_tools(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.api.test_mcp_gateway import _FakeMcp, _seed, _token

    _FakeMcp.calls = []
    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
    token = _token(tenant, agent_id, run_id)

    listed = await _gateway_rpc(_composition(_granted), token, "tools/list")
    names = [t["name"] for t in listed["result"]["tools"]]
    assert "read_document" in names and "create_document" in names
    # The connection's own search_records wins: listed once, never the edition's.
    assert names.count("search_records") == 1

    ok = await _gateway_rpc(
        _composition(_granted), token, "tools/call", {"name": "read_document", "arguments": {}}
    )
    assert ok["result"]["isError"] is False
    assert ok["result"]["content"][0]["text"] == "The document says hello."

    mcp = await _gateway_rpc(
        _composition(_granted), token, "tools/call", {"name": "search_records", "arguments": {}}
    )
    assert mcp["result"]["isError"] is False
    assert [c[0] for c in _FakeMcp.calls] == ["search_records"]

    refused = await _gateway_rpc(
        _composition(_none), token, "tools/call", {"name": "read_document", "arguments": {}}
    )
    assert refused["result"]["isError"] is True
    assert "unknown tool" in refused["result"]["content"][0]["text"]
    assert RAN == ["read_document"], "neither the unentitled call nor the MCP name ran it"

    async with app_session(tenant) as db:
        audit = await _audit(db)
    assert [a.resource["allowed"] for a in audit] == [True, False]


async def test_the_gateway_refuses_a_writing_edition_tool_in_plan(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.api.test_mcp_gateway import _FakeMcp, _seed, _token

    monkeypatch.setattr("oc8.agent.mcp_client.McpSession", _FakeMcp)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent_id, run_id, _t = await _seed(db, tenant)
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        run.context = {**run.context, "chat_mode": "plan"}
        await db.flush()
    token = _token(tenant, agent_id, run_id)
    listed = await _gateway_rpc(_composition(_granted), token, "tools/list")
    names = {t["name"] for t in listed["result"]["tools"]}
    assert "read_document" in names and "create_document" not in names
    called = await _gateway_rpc(
        _composition(_granted), token, "tools/call", {"name": "create_document", "arguments": {}}
    )
    assert called["result"]["isError"] is True
    assert RAN == []


# ------------------------------------------------- isolated control plane


async def _post_tool(
    composition: RuntimeComposition,
    tenant: uuid.UUID,
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    name: str,
) -> dict[str, Any]:
    from tests.api.test_internal_agent import _agent_token

    from oc8.main import create_app

    app = create_app(runtime_composition=composition)
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/tool",
                json={"id": "c1", "name": name, "arguments": {}},
                headers={"Authorization": f"Bearer {_agent_token(tenant, agent_id, run_id)}"},
            )
            assert r.status_code == 200, r.text
            return dict(r.json())


async def test_the_isolated_control_plane_gates_edition_tools(
    app_session: AppSessionFactory,
) -> None:
    from tests.api.test_internal_agent import _plain_agent_run

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id

    refused = await _post_tool(_composition(_none), tenant, agent_id, run_id, "create_document")
    assert refused["status"] == "denied" and "unknown tool" in refused["output"]
    assert RAN == []

    ran = await _post_tool(_composition(_granted), tenant, agent_id, run_id, "create_document")
    assert ran["status"] == "ok" and ran["output"].endswith("Document created.")
    assert RAN == ["create_document"]

    async with app_session(tenant) as db:
        row = await db.get(m.AgentRun, run_id)
        assert row is not None
        # Survives the endpoint's whole-context write-back.
        assert row.context["parts"] == [{"type": "document_ref", "id": "d1"}]
        audit = await _audit(db)
    assert [a.resource["allowed"] for a in audit] == [False, True]
