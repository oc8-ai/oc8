"""The isolated-agent internal API: token scoping + transcript serde. The full
container round-trip is exercised live (needs Docker), not here."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from oc8.api.v1.internal_agent import RUN_SCOPE, _from_message, _to_messages
from oc8.modelrouter import NeutralMessage, ToolCall, chunk_from_result
from oc8.modelrouter.types import ImagePart, TextPart


def _as_stream(fake_complete: Any) -> Any:
    """Wrap a fake `fake_complete(...) -> CompletionResult` callable so it can
    be monkeypatched onto `stream_completion_with_fallback` -- an async
    generator function of the same call signature, yielding the ONE chunk a
    non-streaming fake's whole answer becomes. Same synthesis
    stream_with_fallback itself does for any adapter without `.stream()`."""

    async def _stream(*args: object, **kw: object) -> Any:
        yield chunk_from_result(await fake_complete(*args, **kw))

    return _stream


def test_transcript_serde_round_trips() -> None:
    msgs = [
        NeutralMessage(role="system", content="sys"),
        NeutralMessage(role="user", content="do it"),
        NeutralMessage(
            role="assistant", content="",
            tool_calls=[ToolCall(id="c1", name="search_records", arguments={"model": "crm.lead"})],
        ),
        NeutralMessage(role="tool", content="result", tool_call_id="c1", name="search_records"),
    ]
    raw = [_from_message(m) for m in msgs]
    back = _to_messages(raw)
    assert [m.role for m in back] == ["system", "user", "assistant", "tool"]
    assert back[2].tool_calls[0].name == "search_records"
    assert back[2].tool_calls[0].arguments == {"model": "crm.lead"}
    assert back[3].tool_call_id == "c1" and back[3].name == "search_records"


def test_transcript_serde_round_trips_image_content_through_real_json() -> None:
    """A vision-enabled first step's transcript can hold list `content` mixing
    text and image parts -- this must survive an ACTUAL json.dumps/json.loads
    round trip (not just calling the serde helpers back-to-back in Python),
    since that's exactly what committing the transcript to the run's JSONB
    `context` column does between every /step + /tool request in this
    endpoint's stateless loop. `ImagePart.data` is raw bytes, which plain
    json.dumps cannot serialize at all without the base64 encode/decode in
    `_content_to_json`/`_content_from_json`."""
    image_bytes = bytes(range(256))
    msgs = [
        NeutralMessage(role="system", content="sys"),
        NeutralMessage(
            role="user",
            content=[
                TextPart(text="what's in this image?"),
                ImagePart(data=image_bytes, content_type="image/png"),
            ],
        ),
    ]
    raw = [_from_message(m) for m in msgs]
    # The real persist path: an actual JSON round trip, standing in for the
    # JSONB `context` commit/reload -- not just re-calling the helpers.
    persisted = json.loads(json.dumps(raw))
    back = _to_messages(persisted)

    assert back[0].role == "system"
    assert back[0].content == "sys"
    assert back[1].role == "user"
    assert back[1].content == [
        TextPart(text="what's in this image?"),
        ImagePart(data=image_bytes, content_type="image/png"),
    ]


def test_run_scope_is_the_exact_run() -> None:
    rid = uuid.uuid4()
    assert f"{RUN_SCOPE}{rid}" == f"run:{rid}"
    # A token scoped to another run must not match this one.
    assert f"{RUN_SCOPE}{uuid.uuid4()}" != f"{RUN_SCOPE}{rid}"


@pytest.mark.asyncio
async def test_endpoint_refuses_a_non_agent_token(app_session: object) -> None:
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.auth import get_identity_provider
    from oc8.main import create_app

    tenant = uuid.uuid4()
    rid = uuid.uuid4()
    # An operator token (kind=operator) must be refused by the internal API even
    # with the right run scope — only kind=agent shells may drive a run.
    op = get_identity_provider().mint(
        tenant_id=tenant, subject="op", role="org_admin", scopes=[f"run:{rid}"]
    )
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{rid}/step",
                headers={"Authorization": f"Bearer {op}"},
            )
            assert r.status_code == 403


@pytest.mark.asyncio
async def test_a_step_is_metered(app_session: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """An isolated run's model turns happen HERE, so they must be billed here --
    otherwise a deployment on OC8_AGENT_ISOLATION=true meters nothing and its
    budgets stay empty forever."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import func, select

    from oc8 import models as m
    from oc8.auth import get_identity_provider
    from oc8.main import create_app
    from oc8.modelrouter.types import CompletionResult, Usage
    from oc8.runtime.states import RunState

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Nora", status="running",
            narrowing={}, definition={},
        )
        db.add(agent)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id, state=RunState.RUNNING.value,
            context={"task": "verkauf etwas"},
        )
        db.add(run)
        await db.flush()
        run_id, dept_id = run.id, dept.id

    async def fake_complete(*args: object, **kw: object) -> CompletionResult:
        return CompletionResult(
            text="fertig", tool_calls=[], usage=Usage(tokens_in=120, tokens_out=30),
            stop_reason="stop", provider="openai_compatible", model="opaas_ai:odoo-gpt",
        )

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )

    token = get_identity_provider().mint(
        tenant_id=tenant, subject=f"agent:{agent.id}", role="agent_default", kind="agent",
        scopes=[f"run:{run_id}"],
    )
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text

    async with app_session(tenant) as db:  # type: ignore[operator]
        total = (
            await db.execute(
                select(
                    func.count(),
                    func.coalesce(
                        func.sum(m.TokenUsageRecord.tokens_in + m.TokenUsageRecord.tokens_out), 0
                    ),
                ).where(m.TokenUsageRecord.tenant_id == tenant)
            )
        ).one()
        assert total[0] == 1
        assert total[1] == 150
        row = (
            await db.execute(
                select(m.TokenUsageRecord).where(m.TokenUsageRecord.tenant_id == tenant)
            )
        ).scalar_one()
        # Attributed to the agent AND its department, so a department budget sees it.
        assert row.agent_id == agent.id and row.department_id == dept_id


# ------------------------------------------------------- parity with in-process


def _agent_token(tenant: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID) -> str:
    from oc8.auth import get_identity_provider

    return get_identity_provider().mint(
        tenant_id=tenant, subject=f"agent:{agent_id}", role="agent_default",
        kind="agent", scopes=[f"run:{run_id}"],
    )


async def _seed_lead_with_mate_and_skill(db: Any, tenant: uuid.UUID) -> tuple[Any, Any, Any]:
    from oc8 import models as m
    from oc8.runtime.states import RunState

    dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    db.add(dept)
    await db.flush()
    lead = m.Agent(
        tenant_id=tenant, department_id=dept.id, name="Nora", role_title="Lead",
        status="running", narrowing={}, definition={}, presentation={}, is_team_lead=True,
    )
    mate = m.Agent(
        tenant_id=tenant, department_id=dept.id, name="Rico", status="idle",
        narrowing={}, definition={}, presentation={},
    )
    skill = m.Skill(
        tenant_id=tenant, name="Angebots-Check", description="Pruft Angebote.",
        author="oc8 core",
    )
    db.add_all([lead, mate, skill])
    await db.flush()
    version = m.SkillVersion(
        tenant_id=tenant, skill_id=skill.id, semver="1.0.0", artifact_hash=b"\x00" * 32,
        definition={
            "oc8_skill": 1, "id": "sk-angebot", "version": "1.0.0",
            "instruction": "Prufe das Angebot.",
            "requires": {"tools": [{"tool": "read_record", "rights": ["read"]}], "kbs": []},
            "guardrails": [],
        },
    )
    db.add(version)
    await db.flush()
    skill.current_version_id = version.id
    db.add(m.SkillAssignment(
        tenant_id=tenant, agent_id=lead.id, skill_version_id=version.id, enabled=True
    ))
    run = m.AgentRun(
        tenant_id=tenant, agent_id=lead.id, state=RunState.RUNNING.value,
        context={"task": "Erstelle ein Angebot fuer Kunde X"},
    )
    db.add(run)
    await db.flush()
    return lead, mate, run


@pytest.mark.asyncio
async def test_step_offers_the_same_tools_as_the_in_process_engine(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE regression guard for this slice. The isolated path had drifted to
    offering only MCP tools -- no memory_write, no ask_user, no delegate_task, no
    skills. If the two lists ever diverge again, this test says so."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8 import models as m
    from oc8.agent.control_tools import offered_tools
    from oc8.agent.preamble import build_run_preamble
    from oc8.main import create_app
    from oc8.modelrouter.types import CompletionResult, Usage

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        lead, _mate, run = await _seed_lead_with_mate_and_skill(db, tenant)
        lead_id, run_id = lead.id, run.id

    seen: dict[str, Any] = {}

    async def fake_complete(*args: object, **kw: object) -> CompletionResult:
        seen["tools"] = kw["tools"]
        seen["messages"] = kw["messages"]
        return CompletionResult(
            text="fertig", tool_calls=[], usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop", provider="ollama", model="m",
        )

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )
    token = _agent_token(tenant, lead_id, run_id)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text

    offered_isolated = sorted(t.name for t in seen["tools"])

    async with app_session(tenant) as db:  # type: ignore[operator]
        agent = await db.get(m.Agent, lead_id)
        assert agent is not None
        pre = await build_run_preamble(
            db, agent=agent, tenant_id=tenant, task_text="Erstelle ein Angebot fuer Kunde X",
            frame={}, model_locality="eu",
        )
        offered_in_process = sorted(
            t.name for t in offered_tools(
                agent, assigned_skills=pre.assigned_skills, active_skills=[], mcp_tools=[],
                # The in-process engine always passes this (it has no workspace
                # mount); this agent has no runtime_ref set either, so it falls
                # back to the builtin isolated shell -- also with no local
                # filesystem -- and must offer the same tool for parity.
                offer_write_output_file=True,
                # Deliberately NOT offer_run_shell=True here: engine.py's
                # loop()._offered() never passes it either. Only the
                # isolated runtime's isolated_shell.py can pre-execute a
                # command locally and hand back a local_result -- the
                # in-process engine has no equivalent, so real production
                # in-process runs correctly never offer run_shell. That
                # asymmetry is asserted explicitly below instead of being
                # papered over by a synthetic match.
            )
        )

    # run_shell is the one intentional asymmetry between the two runtimes
    # (see the comment above) -- assert it directly rather than forcing the
    # two lists to artificially match.
    assert "run_shell" in offered_isolated
    assert "run_shell" not in offered_in_process
    assert [n for n in offered_isolated if n != "run_shell"] == offered_in_process
    assert "memory_write" in offered_isolated
    assert "ask_user" in offered_isolated
    assert "delegate_task" in offered_isolated, "a team lead must be able to delegate"
    assert any(n.startswith("skill_") for n in offered_isolated), "skills must be invocable"
    assert "write_output_file" in offered_isolated


@pytest.mark.asyncio
async def test_step_withholds_write_output_file_from_a_real_runtime_plugin(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real runtime plugin (e.g. claude_code_runtime) has its own local file
    tools inside its container -- offering write_output_file too would be
    redundant. Only the builtin isolated shell (or an unset runtime_ref, which
    falls back to it) has no filesystem of its own and needs the tool."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app
    from oc8.modelrouter.types import CompletionResult, Usage

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        lead, _mate, run = await _seed_lead_with_mate_and_skill(db, tenant)
        lead.runtime_ref = str(uuid.uuid4())
        lead_id, run_id = lead.id, run.id

    seen: dict[str, Any] = {}

    async def fake_complete(*args: object, **kw: object) -> CompletionResult:
        seen["tools"] = kw["tools"]
        return CompletionResult(
            text="fertig", tool_calls=[], usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop", provider="ollama", model="m",
        )

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )
    token = _agent_token(tenant, lead_id, run_id)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text

    offered = {t.name for t in seen["tools"]}
    assert "write_output_file" not in offered


@pytest.mark.asyncio
async def test_step_seeds_the_roster_so_delegation_can_name_a_real_agent(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offering delegate_task without telling the agent who its colleagues are
    would mean every delegation is denied as an invalid agent_id."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app
    from oc8.modelrouter.types import CompletionResult, Usage

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        lead, mate, run = await _seed_lead_with_mate_and_skill(db, tenant)
        lead_id, mate_id, run_id = lead.id, mate.id, run.id

    seen: dict[str, Any] = {}

    async def fake_complete(*args: object, **kw: object) -> CompletionResult:
        seen["messages"] = kw["messages"]
        return CompletionResult(
            text="fertig", tool_calls=[], usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop", provider="ollama", model="m",
        )

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )
    token = _agent_token(tenant, lead_id, run_id)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text

    seeded = "\n".join(msg.content for msg in seen["messages"])
    assert str(mate_id) in seeded, "the roster must be seeded"
    assert "Angebots-Check" in seeded, "the skills catalog must be seeded"
    assert "Erstelle ein Angebot fuer Kunde X" in seeded


# ------------------------------------------------- control tools under isolation


async def _post_tool(
    tenant: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID,
    name: str, arguments: dict[str, Any],
    local_result: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app

    token = _agent_token(tenant, agent_id, run_id)
    body: dict[str, Any] = {"id": "c1", "name": name, "arguments": arguments}
    if local_result is not None:
        body["local_result"] = local_result
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/tool",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.status_code, (r.json() if r.content else {})


async def _plain_agent_run(
    db: Any, tenant: uuid.UUID, **run_ctx: Any
) -> tuple[Any, Any, Any]:
    """An agent and a RUNNING run with a task already open, and NO MCP
    connection bound -- a control tool must work without one."""
    from oc8 import models as m
    from oc8.runtime.states import RunState

    dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant, department_id=dept.id, name="Nora", status="running",
        narrowing={}, definition={}, presentation={},
    )
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant, department_id=dept.id, assigned_agent_id=agent.id,
        title="Erstelle ein Angebot", state="in_progress",
    )
    db.add(task)
    await db.flush()
    run = m.AgentRun(
        tenant_id=tenant, agent_id=agent.id, task_id=task.id, state=RunState.RUNNING.value,
        context={"task": "Erstelle ein Angebot", **run_ctx},
    )
    db.add(run)
    await db.flush()
    return agent, task, run


@pytest.mark.asyncio
async def test_memory_write_needs_no_mcp_connection(app_session: object) -> None:
    """The endpoint used to 409 when no connection was bound, which made every
    core-owned tool unusable on a run without one."""
    from sqlalchemy import func, select

    from oc8 import models as m

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id

    code, body = await _post_tool(
        tenant, agent_id, run_id, "memory_write",
        {"tier": "agent", "content": "Kunde zahlt per Rechnung."},
    )
    assert code == 200, body
    assert body["status"] == "ok"

    async with app_session(tenant) as db:  # type: ignore[operator]
        count = (
            await db.execute(
                select(func.count()).select_from(m.MemoryRecord).where(
                    m.MemoryRecord.tenant_id == tenant
                )
            )
        ).scalar_one()
        assert count == 1


@pytest.mark.asyncio
async def test_ask_user_suspends_the_run_for_a_clarification(app_session: object) -> None:
    """The shell already stops on waiting_for_input and isolated.py already maps
    it -- nothing ever produced it, so an isolated agent could never ask."""
    from oc8 import models as m

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id

    code, body = await _post_tool(
        tenant, agent_id, run_id, "ask_user", {"question": "Welches Konto soll ich nehmen?"}
    )
    assert code == 200, body
    assert body["status"] == "waiting_for_input"
    assert body["output"] == "Welches Konto soll ich nehmen?"

    async with app_session(tenant) as db:  # type: ignore[operator]
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        # The verdict the runtime reads after the container exits.
        assert run.context["isolated_result"]["status"] == "waiting_for_input"
        assert run.context["isolated_result"]["output"] == "Welches Konto soll ich nehmen?"


@pytest.mark.asyncio
async def test_an_empty_question_does_not_suspend_the_run(app_session: object) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id

    code, body = await _post_tool(tenant, agent_id, run_id, "ask_user", {"question": "  "})
    assert code == 200, body
    assert body["status"] == "ok"
    assert body["output"].startswith("ERROR:")


@pytest.mark.asyncio
async def test_delegation_at_the_depth_cap_is_denied(app_session: object) -> None:
    """Proves delegation_depth now reaches _authorize. It used to default to 0 on
    this path, so the cap compared against 0 and a chain could run past it."""
    from oc8 import models as m
    from oc8.agent.control_tools import MAX_DELEGATION_DEPTH

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(
            db, tenant, delegation_depth=MAX_DELEGATION_DEPTH
        )
        agent.is_team_lead = True
        mate = m.Agent(
            tenant_id=tenant, department_id=agent.department_id, name="Rico",
            status="idle", narrowing={}, definition={}, presentation={},
        )
        db.add(mate)
        await db.flush()
        agent_id, mate_id, run_id = agent.id, mate.id, run.id

    code, body = await _post_tool(
        tenant, agent_id, run_id, "delegate_task",
        {"agent_id": str(mate_id), "task_text": "Ruf den Kunden an"},
    )
    assert code == 200, body
    assert body["status"] == "denied"
    assert "depth limit" in body["output"]

    async with app_session(tenant) as db:  # type: ignore[operator]
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.context.get("pending_runs", []) == [], "no sub-run may be created"


@pytest.mark.asyncio
async def test_a_delegated_sub_run_is_recorded_for_the_executor_to_publish(
    app_session: object,
) -> None:
    """run_agent must never publish a sub-run itself; the isolated path likewise
    records it and lets the executor publish after committing."""
    from oc8 import models as m

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent.is_team_lead = True
        mate = m.Agent(
            tenant_id=tenant, department_id=agent.department_id, name="Rico",
            status="idle", narrowing={}, definition={}, presentation={},
        )
        db.add(mate)
        await db.flush()
        agent_id, mate_id, run_id = agent.id, mate.id, run.id

    code, body = await _post_tool(
        tenant, agent_id, run_id, "delegate_task",
        {"agent_id": str(mate_id), "task_text": "Ruf den Kunden an"},
    )
    assert code == 200, body
    assert body["status"] == "ok"

    async with app_session(tenant) as db:  # type: ignore[operator]
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        pending = run.context.get("pending_runs", [])
        assert len(pending) == 1
        sub = await db.get(m.AgentRun, uuid.UUID(pending[0]))
        assert sub is not None and sub.agent_id == mate_id


@pytest.mark.asyncio
async def test_invoking_an_assigned_skill_activates_it_across_steps(
    app_session: object,
) -> None:
    """Proves skill_tool_names now reaches _authorize (else the frame check would
    deny it), and that activation survives into the NEXT request -- there is no
    in-memory loop here to hold it."""
    from oc8 import models as m
    from oc8.skills.runtime import load_assigned_skills

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        lead, _mate, run = await _seed_lead_with_mate_and_skill(db, tenant)
        task = m.Task(
            tenant_id=tenant, department_id=lead.department_id, assigned_agent_id=lead.id,
            title="Erstelle ein Angebot", state="in_progress",
        )
        db.add(task)
        await db.flush()
        run.task_id = task.id
        lead_id, run_id = lead.id, run.id
        # Derived from the definition's slug, not the display name -- ask the
        # loader rather than hardcoding it.
        loaded = await load_assigned_skills(db, agent=lead, tenant_id=tenant)
        tool_name = loaded[0].tool_name

    code, body = await _post_tool(tenant, lead_id, run_id, tool_name, {})
    assert code == 200, body
    assert body["status"] == "ok", body
    assert "activated" in body["output"]

    async with app_session(tenant) as db:  # type: ignore[operator]
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.context.get("active_skill_ids"), "activation must survive the request"
        seeded = "\n".join(msg.get("content", "") for msg in run.context["transcript"])
        assert "Prufe das Angebot." in seeded, "the skill's procedure must reach the model"


@pytest.mark.asyncio
async def test_a_skill_instruction_never_splits_a_tool_call_from_its_result(
    app_session: object,
) -> None:
    """A tool result MUST immediately follow the assistant message that asked for
    it. Appending the activated skill's instruction before the result puts a
    system message between the two, which an OpenAI-compatible provider rejects
    with 400 -- the run dies on the very next step. Found live against
    ai.opaas.online, not in a unit test."""
    from oc8 import models as m
    from oc8.skills.runtime import load_assigned_skills

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        lead, _mate, run = await _seed_lead_with_mate_and_skill(db, tenant)
        task = m.Task(
            tenant_id=tenant, department_id=lead.department_id, assigned_agent_id=lead.id,
            title="Erstelle ein Angebot", state="in_progress",
        )
        db.add(task)
        await db.flush()
        run.task_id = task.id
        # The transcript as /step leaves it: the assistant asked for the skill.
        run.context = {
            **run.context,
            "transcript": [
                {"role": "system", "content": "you are nora"},
                {"role": "user", "content": "Erstelle ein Angebot"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "c1", "name": "skill_angebot", "arguments": {}}],
                },
            ],
        }
        loaded = await load_assigned_skills(db, agent=lead, tenant_id=tenant)
        tool_name = loaded[0].tool_name
        lead_id, run_id = lead.id, run.id

    code, body = await _post_tool(tenant, lead_id, run_id, tool_name, {})
    assert code == 200, body

    async with app_session(tenant) as db:  # type: ignore[operator]
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        roles = [msg["role"] for msg in run.context["transcript"]]

    # No message is inserted at all: the procedure IS the tool result. A strict
    # backend rejects a mid-conversation system message outright ("Unexpected role
    # 'system' after role 'tool'"), so there must be nothing to misplace.
    assert roles == ["system", "user", "assistant", "tool"], roles
    async with app_session(tenant) as db:  # type: ignore[operator]
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert "Prufe das Angebot." in run.context["transcript"][-1]["content"], (
            "the skill's procedure must reach the model, in the tool result"
        )


@pytest.mark.asyncio
async def test_a_repeated_write_does_not_reach_the_tool_server_twice(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotency on the real path (§8.7 R5): a restarted task replaying the same
    write must get the first result, not create a second record. Reads are exempt
    -- deduplicating a search would hide changes the agent is meant to see."""
    from oc8 import models as m

    calls: list[str] = []

    class _CountingSession:
        tools: list[Any] = []

        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def call(self, name: str, arguments: dict[str, Any]) -> str:
            calls.append(name)
            return f"created id={len(calls)}"

    monkeypatch.setattr("oc8.api.v1.internal_agent.McpSession", _CountingSession)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        dept = m.Department(
            tenant_id=tenant, name="Vertrieb",
            frame={"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}},
        )
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Nora", status="running",
            narrowing={}, definition={}, presentation={},
        )
        db.add(agent)
        await db.flush()
        conn = m.McpConnection(
            tenant_id=tenant, department_id=dept.id, name="odoo", transport="stdio",
            server_url="stdio://odoo", connected=True, config={"command": "x", "args": []},
            scopes={"read": ["search_records"], "write": ["create_record"]},
        )
        task = m.Task(
            tenant_id=tenant, department_id=dept.id, assigned_agent_id=agent.id,
            title="Angebot", state="in_progress",
        )
        db.add_all([conn, task])
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id, task_id=task.id,
            state="running", context={"task": "x", "mcp_connection_id": str(conn.id)},
        )
        db.add(run)
        await db.flush()
        agent_id, run_id = agent.id, run.id

    args = {"model": "sale.order", "values": {"partner_id": 7}}
    first_code, first = await _post_tool(tenant, agent_id, run_id, "create_record", args)
    second_code, second = await _post_tool(tenant, agent_id, run_id, "create_record", args)

    assert first_code == 200 and second_code == 200, (first, second)
    assert len(calls) == 1, f"the write reached the tool server {len(calls)} times"
    assert "created id=1" in second["output"]
    assert "replay" in second["output"].lower(), "the agent must be told no second action ran"

    # A read is deliberately NOT deduplicated.
    await _post_tool(tenant, agent_id, run_id, "search_records", {"model": "crm.lead"})
    await _post_tool(tenant, agent_id, run_id, "search_records", {"model": "crm.lead"})
    assert calls.count("search_records") == 2, "reads must not be replayed from cache"


# ------------------------------------------------- tool-call timing (KPIs)


async def _mcp_backed_run(db: Any, tenant: uuid.UUID, *, modify: bool = True) -> tuple[Any, Any]:
    """An agent + RUNNING run with an `odoo` MCP connection bound, the fixture
    the two timing tests below share. Returns (agent_id, run_id)."""
    from oc8 import models as m

    dept = m.Department(
        tenant_id=tenant,
        name="Vertrieb",
        frame={"tools": {"odoo": {"enabled": True, "read": True, "modify": modify}}},
    )
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
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
        name="odoo",
        transport="stdio",
        server_url="stdio://odoo",
        connected=True,
        config={"command": "x", "args": []},
        scopes={"read": ["search_records"], "write": ["create_record"]},
    )
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=agent.id,
        title="Angebot",
        state="in_progress",
    )
    db.add_all([conn, task])
    await db.flush()
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=agent.id,
        task_id=task.id,
        state="running",
        context={"task": "x", "mcp_connection_id": str(conn.id)},
    )
    db.add(run)
    await db.flush()
    return agent.id, run.id


@pytest.mark.asyncio
async def test_a_dispatched_call_records_started_at_and_duration(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Container-runtime parity with `agent/engine.py`'s tool-call timing.

    Without this, `context->'toolCalls'` entries written by THIS endpoint carry
    no `durationMs`, `kpis.aggregate._avg_tool_call_duration_ms` (which counts
    only entries that have the key) sees nothing, and every agent running under
    OC8_AGENT_ISOLATION reports `avgToolCallDurationMs: null` forever -- the
    same "container parity is not automatic" trap the preamble and control-tool
    seams have each hit before.
    """
    from oc8 import models as m

    class _SlowSession:
        tools: list[Any] = []

        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def call(self, name: str, arguments: dict[str, Any]) -> str:
            import asyncio

            # Long enough that a truncating-to-int duration cannot be 0 by
            # accident, so the assertion below measures real elapsed time
            # rather than merely the presence of a key.
            await asyncio.sleep(0.02)
            return "created id=1"

    monkeypatch.setattr("oc8.api.v1.internal_agent.McpSession", _SlowSession)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent_id, run_id = await _mcp_backed_run(db, tenant)

    code, body = await _post_tool(
        tenant,
        agent_id,
        run_id,
        "create_record",
        {"model": "sale.order", "values": {"partner_id": 7}},
    )
    assert code == 200, body

    async with app_session(tenant) as db:  # type: ignore[operator]
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        entry = run.context["toolCalls"][-1]

    assert entry["tool"] == "create_record"
    assert isinstance(entry["startedAt"], str) and entry["startedAt"].endswith("+00:00")
    assert isinstance(entry["durationMs"], int)
    assert entry["durationMs"] >= 20, entry["durationMs"]


@pytest.mark.asyncio
async def test_a_denied_call_records_no_timing(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same rule, mirroring engine.py: a call refused
    before dispatch never reached a tool server, so it carries no duration --
    otherwise every denial would push `avgToolCallDurationMs` toward zero."""
    from oc8 import models as m

    class _NeverSession:
        tools: list[Any] = []

        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def call(self, name: str, arguments: dict[str, Any]) -> str:
            raise AssertionError("a denied call must never reach the tool server")

    monkeypatch.setattr("oc8.api.v1.internal_agent.McpSession", _NeverSession)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        # modify=False -> the frame denies create_record outright.
        agent_id, run_id = await _mcp_backed_run(db, tenant, modify=False)

    code, body = await _post_tool(
        tenant,
        agent_id,
        run_id,
        "create_record",
        {"model": "sale.order", "values": {"partner_id": 7}},
    )
    assert code == 200, body
    assert body["status"] == "denied", body

    async with app_session(tenant) as db:  # type: ignore[operator]
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        entry = run.context["toolCalls"][-1]

    assert entry["result"].startswith("ERROR:")
    assert "startedAt" not in entry
    assert "durationMs" not in entry


# ------------------------------------------------- department prompt caching


@pytest.mark.asyncio
async def test_a_second_identical_first_step_in_the_same_department_is_served_from_cache(
    app_session: object, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Department prompt caching must work on the ISOLATED runtime too. This
    endpoint is the model-call site when OC8_AGENT_ISOLATION=true, so without
    it the department toggle and the Cost page's savings figure render on such
    a deployment and do nothing.

    The two agents share a name on purpose: `preamble.system_prompt()` opens
    with `f"You are {agent.name}"`, so differently-named agents structurally
    can never share an entry (see tests/agents/test_department_cache_wiring.py,
    which makes the same choice for the in-process path)."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select

    from oc8 import models as m
    from oc8.config import get_settings
    from oc8.main import create_app
    from oc8.modelrouter.types import CompletionResult, Usage
    from oc8.runtime.states import RunState

    settings = get_settings()
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        dept = m.Department(
            tenant_id=tenant, name="Support", frame={}, prompt_caching_enabled=True
        )
        db.add(dept)
        await db.flush()
        agents = [
            m.Agent(
                tenant_id=tenant, department_id=dept.id, name="Triage Agent",
                status="running", narrowing={}, definition={}, presentation={},
            )
            for _ in range(2)
        ]
        db.add_all(agents)
        await db.flush()
        runs = [
            m.AgentRun(
                tenant_id=tenant, agent_id=a.id, state=RunState.RUNNING.value,
                context={"task": "classify this ticket"},
            )
            for a in agents
        ]
        db.add_all(runs)
        await db.flush()
        pairs = [(a.id, r.id) for a, r in zip(agents, runs, strict=True)]

    calls = {"n": 0}

    async def fake_complete(*args: object, **kw: object) -> CompletionResult:
        calls["n"] += 1
        # Answer AS the requested model: cache_flow.store_if_matching refuses to
        # cache a result whose provider/model differs from the key's, so a fake
        # answering as something else would never populate the cache at all.
        return CompletionResult(
            text="Triage: billing.", tool_calls=[], usage=Usage(tokens_in=120, tokens_out=40),
            stop_reason="stop",
            provider=settings.default_model_provider, model=settings.default_model,
        )

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            bodies = []
            for agent_id, run_id in pairs:
                r = await c.post(
                    f"/api/v1/internal/agent/{run_id}/step",
                    headers={"Authorization": f"Bearer {_agent_token(tenant, agent_id, run_id)}"},
                )
                assert r.status_code == 200, r.text
                bodies.append(r.json())

    assert calls["n"] == 1, "the second identical first step must be served from cache"
    assert bodies[1]["text"] == bodies[0]["text"]

    # And the hit is metered as a hit: zero actual spend, the avoided spend in
    # the saved_* columns -- the same accounting the in-process engine writes.
    async with app_session(tenant) as db:  # type: ignore[operator]
        rows = (
            (
                await db.execute(
                    select(m.TokenUsageRecord)
                    .where(m.TokenUsageRecord.tenant_id == tenant)
                    .order_by(m.TokenUsageRecord.ts)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2
    original, hit = rows
    assert original.cache_hit is False
    assert hit.cache_hit is True
    assert hit.tokens_in == 0 and hit.tokens_out == 0
    assert hit.saved_tokens_in == 120 and hit.saved_tokens_out == 40


@pytest.mark.asyncio
async def test_restricted_content_reaches_the_model_call_on_every_step(
    app_session: object, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`contains_restricted` used to be hardcoded False here, so the isolated
    runtime never enforced the restricted-content routing (forced local model)
    that the in-process engine enforces -- and could have cached restricted
    content. The preamble computes it only on a run's FIRST step, so it is
    persisted into the run context and read back on every later step; both
    halves are asserted."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select

    from oc8 import models as m
    from oc8.knowledge.ingest import ingest_document
    from oc8.main import create_app
    from oc8.modelrouter.types import CompletionResult, Usage
    from oc8.models.knowledge import EMBED_DIM
    from oc8.runtime.states import RunState

    class _FakeEmbedRouter:
        async def embed(self, text: str, model: str | None = None) -> list[float]:
            seed = sum(ord(ch) for ch in text) or 1
            return [float((seed + i) % 23) for i in range(EMBED_DIM)]

    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        dept = m.Department(
            tenant_id=tenant,
            name="Legal",
            frame={"cleared_classes": ["public", "internal", "confidential", "restricted"]},
            prompt_caching_enabled=True,
        )
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Agent R", status="running",
            narrowing={}, definition={}, presentation={},
        )
        db.add(agent)
        await db.flush()
        kb = m.KnowledgeBase(tenant_id=tenant, name="Legal", embedding_model="nomic-embed-text")
        db.add(kb)
        await db.flush()
        await ingest_document(
            db, tenant_id=tenant, kb_id=kb.id, filename="a.txt",
            content="top secret merger terms", content_type="text/plain",
        )
        chunk = (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id))).scalar_one()
        chunk.classification = "restricted"
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        run = m.AgentRun(
            tenant_id=tenant, agent_id=agent.id, state=RunState.RUNNING.value,
            context={"task": "merger terms"},
        )
        db.add(run)
        await db.flush()
        agent_id, run_id = agent.id, run.id

    seen: list[object] = []

    async def fake_complete(*args: object, **kw: object) -> CompletionResult:
        seen.append(kw["contains_restricted"])
        return CompletionResult(
            text="ok", tool_calls=[], usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop", provider="ollama", model="m",
        )

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            headers = {"Authorization": f"Bearer {_agent_token(tenant, agent_id, run_id)}"}
            for _ in range(2):
                r = await c.post(f"/api/v1/internal/agent/{run_id}/step", headers=headers)
                assert r.status_code == 200, r.text

    assert seen == [True, True], (
        "the restricted flag must reach the model call on the first step AND on "
        "every later step, which only works because it is persisted into the run "
        "context (the preamble runs once)"
    )
    async with app_session(tenant) as db:  # type: ignore[operator]
        run_row = await db.get(m.AgentRun, run_id)
        assert run_row is not None
        assert run_row.context["contains_restricted"] is True


@pytest.mark.asyncio
async def test_step_persists_and_reoffers_copilot_permissions_across_calls(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The isolated-container runtime cannot close over a local variable
    across HTTP calls the way engine.py's in-process loop can -- this proves
    the round-trip through ctx survives a second /step call."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8 import models as m
    from oc8.main import create_app
    from oc8.modelrouter.types import CompletionResult, Usage
    from oc8.runtime.states import RunState

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        dept = m.Department(tenant_id=tenant, name="oc8 Assistant", frame={})
        db.add(dept)
        await db.flush()
        assistant = m.Agent(
            tenant_id=tenant, department_id=dept.id, name="Assistant", status="running",
            narrowing={}, definition={}, presentation={}, is_team_lead=True,
            is_tenant_assistant=True,
        )
        db.add(assistant)
        await db.flush()
        task = m.Task(
            tenant_id=tenant, department_id=dept.id, assigned_agent_id=assistant.id,
            title="Chat", state="in_progress",
        )
        db.add(task)
        await db.flush()
        run = m.AgentRun(
            tenant_id=tenant, agent_id=assistant.id, task_id=task.id,
            state=RunState.RUNNING.value, context={"task": "Hallo"},
        )
        db.add(run)
        await db.flush()
        # `all_departments=True` alone (no role, no seat) is enough here:
        # `_gated_copilot_permissions` only ever calls
        # `agent_actor.scope.holds_anywhere(permission)`, and `scope_for_member`
        # makes `all_departments` set `_unrestricted`, which is exactly what
        # `holds_anywhere(perm(APPROVAL, VIEW))` reads.
        member = m.OrgMember(
            tenant_id=tenant, subject=f"anna-{uuid.uuid4()}", subject_uuid=uuid.uuid4(),
            all_departments=True,
        )
        db.add(member)
        await db.flush()
        db.add(
            m.ChatSession(
                tenant_id=tenant, agent_id=assistant.id, member_id=member.id, task_id=task.id
            )
        )
        await db.flush()
        agent_id, run_id = assistant.id, run.id

    seen: list[list[str]] = []

    async def fake_complete(*args: object, **kw: object) -> CompletionResult:
        seen.append([t.name for t in kw["tools"]])
        return CompletionResult(
            text="ok", tool_calls=[], usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop", provider="ollama", model="m",
        )

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            headers = {"Authorization": f"Bearer {_agent_token(tenant, agent_id, run_id)}"}
            for _ in range(2):
                r = await c.post(f"/api/v1/internal/agent/{run_id}/step", headers=headers)
                assert r.status_code == 200, r.text

    assert len(seen) == 2
    assert "list_pending_approvals" in seen[0], (
        "copilot_permissions computed by the preamble on the FIRST step must "
        "reach offered_tools immediately"
    )
    assert "list_pending_approvals" in seen[1], (
        "and it must still be offered on the SECOND step, which only works "
        "because it is persisted into ctx -- the isolated runtime has no "
        "Python closure to hold it across separate HTTP calls"
    )


@pytest.mark.asyncio
async def test_the_internal_endpoint_publishes_a_rendered_component(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8 import models as m

    published: list[tuple[str, dict[str, Any]]] = []

    class _SpyBus:
        async def publish_event(
            self, tenant_id: Any, type_: str, data: dict[str, Any], **kw: Any
        ) -> None:
            published.append((type_, data))

    monkeypatch.setattr("oc8.realtime.bus.get_event_bus", lambda: _SpyBus())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id
        db.add(
            m.ComponentGrant(
                tenant_id=tenant,
                component_key="record_card",
                grantee_type="agent",
                grantee_id=agent_id,
            )
        )
        await db.commit()

    code, body = await _post_tool(
        tenant,
        agent_id,
        run_id,
        "render_component",
        {"component_key": "record_card", "props": {"title": "Acme"}},
    )
    assert code == 200, body
    assert (
        "run.component_rendered",
        {
            "run_id": str(run_id),
            "component_key": "record_card",
            "props": {
                "title": "Acme",
                "subtitle": None,
                "fields": [],
                "link_label": None,
                "link_url": None,
            },
        },
    ) in published


@pytest.mark.asyncio
async def test_the_internal_endpoint_publishes_todos(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parity with the in-process engine's todos fix: without this, an
    already-open Live Log tab watching an isolated/container run only ever
    sees the todos present at its initial GET /runs/{id} fetch (see
    frontend/src/lib/live/apply-event.ts's `run.todos_updated` patcher)."""
    published: list[tuple[str, dict[str, Any]]] = []

    class _SpyBus:
        async def publish_event(
            self, tenant_id: Any, type_: str, data: dict[str, Any], **kw: Any
        ) -> None:
            published.append((type_, data))

    monkeypatch.setattr("oc8.realtime.bus.get_event_bus", lambda: _SpyBus())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id

    code, body = await _post_tool(
        tenant,
        agent_id,
        run_id,
        "todo_write",
        {"todos": [{"content": "Check it", "status": "pending"}]},
    )
    assert code == 200, body
    assert (
        "run.todos_updated",
        {"run_id": str(run_id), "todos": [{"content": "Check it", "status": "pending"}]},
    ) in published


@pytest.mark.asyncio
async def test_the_internal_endpoint_makes_a_tool_call_live(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parity with the in-process engine's Live Log fix (agent/engine.py's
    _live_tool_call, 2026-08-25): the isolated/container runtime's own
    POST .../tool must ALSO append each call to context["toolCalls"]
    immediately (not just to "transcript", which is all this endpoint
    persisted before) and publish run.tool_call, so an open Live Log tab
    and a fresh mid-run page load behave identically regardless of which
    runtime an agent uses."""
    from oc8 import models as m

    published: list[tuple[str, dict[str, Any]]] = []

    class _SpyBus:
        async def publish_event(
            self, tenant_id: Any, type_: str, data: dict[str, Any], **kw: Any
        ) -> None:
            published.append((type_, data))

    # oc8.realtime.emit's publish_run_tool_call (called by the code under
    # test, unlike run.component_rendered's own lazy call-site import a few
    # lines up in internal_agent.py) resolves get_event_bus via a MODULE-
    # LEVEL `from oc8.realtime.bus import get_event_bus` -- a name already
    # bound in emit.py's own namespace at import time, which patching the
    # `bus` module's attribute afterward does not reach. Patch emit's own
    # reference too, or this spy silently never sees run.tool_call.
    monkeypatch.setattr("oc8.realtime.bus.get_event_bus", lambda: _SpyBus())
    monkeypatch.setattr("oc8.realtime.emit.get_event_bus", lambda: _SpyBus())
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id
        db.add(
            m.ComponentGrant(
                tenant_id=tenant,
                component_key="record_card",
                grantee_type="agent",
                grantee_id=agent_id,
            )
        )
        await db.commit()

    code, body = await _post_tool(
        tenant,
        agent_id,
        run_id,
        "render_component",
        {"component_key": "record_card", "props": {"title": "Acme"}},
    )
    assert code == 200, body

    expected_call = {
        "tool": "render_component",
        "arguments": {"component_key": "record_card", "props": {"title": "Acme"}},
        "result": body["output"][:300],
    }
    #: `startedAt`/`durationMs` ride along on a dispatched call (KPI timing, see
    #: test_a_dispatched_call_records_started_at_and_duration below); this test
    #: is about the call being live at all, so it compares the rest by name
    #: rather than pinning a wall-clock value.
    tool_call_events = [data for type_, data in published if type_ == "run.tool_call"]
    assert len(tool_call_events) == 1
    assert tool_call_events[0]["run_id"] == str(run_id)
    published_call = tool_call_events[0]["call"]
    assert {k: v for k, v in published_call.items() if k in expected_call} == expected_call

    async with app_session(tenant) as db:  # type: ignore[operator]
        fresh = await db.get(m.AgentRun, run_id)
        assert fresh is not None
        stored = fresh.context["toolCalls"]
        assert len(stored) == 1, (
            "a mid-run page load must already see this tool call, not just an "
            "open Live Log tab -- the same invariant Stage 1 already proved "
            "for the in-process engine"
        )
        assert {k: v for k, v in stored[0].items() if k in expected_call} == expected_call


@pytest.mark.asyncio
async def test_the_internal_endpoints_step_streams_token_deltas_live(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 2 parity with the in-process engine's own streaming fix
    (agent/engine.py's _live_token_delta): the isolated runtime's own
    POST .../step must publish run.token_delta as each text fragment of the
    model's turn arrives, not only return the whole answer once the call
    finishes -- so a Live Log tab sees an isolated agent's answer form word
    by word exactly like it already does for the Standard runtime."""
    from oc8.modelrouter.types import CompletionChunk, Usage

    published: list[tuple[str, dict[str, Any]]] = []

    class _SpyBus:
        async def publish_event(
            self, tenant_id: Any, type_: str, data: dict[str, Any], **kw: Any
        ) -> None:
            published.append((type_, data))

    # Same module-level-import scoping hazard as the tool_call test above:
    # publish_run_token_delta lives in oc8.realtime.emit, which resolves
    # get_event_bus via its own module-level import.
    monkeypatch.setattr("oc8.realtime.bus.get_event_bus", lambda: _SpyBus())
    monkeypatch.setattr("oc8.realtime.emit.get_event_bus", lambda: _SpyBus())

    async def fake_stream(*args: object, **kw: object) -> Any:
        yield CompletionChunk(text="Hal", provider="openai_compatible", model="opaas_ai:odoo-gpt")
        yield CompletionChunk(text="lo")
        yield CompletionChunk(usage=Usage(tokens_in=10, tokens_out=2), stop_reason="stop")

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", fake_stream
    )

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id

    token = _agent_token(tenant, agent_id, run_id)
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["text"] == "Hallo"

    deltas = [data["text"] for type_, data in published if type_ == "run.token_delta"]
    assert deltas == ["Hal", "lo"]


# ------------------------------------------------------- length truncation
# See tests/agents/test_length_truncation.py for the in-process engine's
# identical behaviour and the live incident that motivated both.


@pytest.mark.asyncio
async def test_step_retries_an_empty_length_truncation_with_a_bigger_budget(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.modelrouter.types import CompletionResult, Usage

    seen_max_tokens: list[int] = []

    async def fake_complete(*args: object, **kw: object) -> CompletionResult:
        params = kw.get("params")
        seen_max_tokens.append(getattr(params, "max_tokens", -1))
        if len(seen_max_tokens) == 1:
            return CompletionResult(
                text="", tool_calls=[], usage=Usage(tokens_in=6832, tokens_out=1536),
                stop_reason="length", provider="openrouter", model="z-ai/glm-5.3-flash",
            )
        return CompletionResult(
            text="Approval code BIOS-7743-QUARTZ noted.", tool_calls=[],
            usage=Usage(tokens_in=7000, tokens_out=40), stop_reason="stop",
            provider="openrouter", model="z-ai/glm-5.3-flash",
        )

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id

    token = _agent_token(tenant, agent_id, run_id)
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status_override"] is None
            assert "BIOS-7743-QUARTZ" in body["text"]
    assert len(seen_max_tokens) == 2
    assert seen_max_tokens[1] > seen_max_tokens[0]


@pytest.mark.asyncio
async def test_step_reports_status_override_failed_when_still_truncated_after_retry(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.modelrouter.types import CompletionResult, Usage

    calls = {"n": 0}

    async def fake_complete(*args: object, **kw: object) -> CompletionResult:
        calls["n"] += 1
        return CompletionResult(
            text="", tool_calls=[], usage=Usage(tokens_in=8000, tokens_out=1536),
            stop_reason="length", provider="openrouter", model="z-ai/glm-5.3-flash",
        )

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id

    token = _agent_token(tenant, agent_id, run_id)
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["status_override"] == "failed"
    # Exactly one retry, never an unbounded loop.
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_step_nudges_instead_of_finishing_when_todos_are_still_open(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Container-parity proof for engine.py's todo-continuation gate: the
    isolated runtime must never let the shell see an intermediate "no tool
    calls yet" response while ctx["todos"] still has an open item. Here the
    nudge works -- the model's next turn actually calls todo_write -- so the
    internal retry loop hands that real tool call straight back to the shell
    (only a genuine no-tool-calls attempt to finish gets nudged again;
    dispatching a real tool call is /tool's job, not /step's)."""
    from oc8.modelrouter.types import CompletionResult, Usage

    calls = {"n": 0}

    async def fake_complete(*args: object, **kw: object) -> CompletionResult:
        calls["n"] += 1
        if calls["n"] == 1:
            return CompletionResult(
                text="Ticket A is resolved.", tool_calls=[],
                usage=Usage(tokens_in=100, tokens_out=20), stop_reason="stop",
                provider="openrouter", model="z-ai/glm-5.3-flash",
            )
        assert "Resolve ticket B" in str(kw["messages"][-1].content)  # type: ignore[index]
        return CompletionResult(
            text="",
            tool_calls=[
                ToolCall(
                    id="c1", name="todo_write",
                    arguments={"todos": [{"content": "Resolve ticket B", "status": "completed"}]},
                )
            ],
            usage=Usage(tokens_in=120, tokens_out=20), stop_reason="tool_calls",
            provider="openrouter", model="z-ai/glm-5.3-flash",
        )

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(
            db, tenant, todos=[{"content": "Resolve ticket B", "status": "pending"}]
        )
        agent_id, run_id = agent.id, run.id

    token = _agent_token(tenant, agent_id, run_id)
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8 import models as m
    from oc8.main import create_app

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["done"] is False
            assert body["tool_calls"] == [
                {
                    "id": "c1",
                    "name": "todo_write",
                    "arguments": {
                        "todos": [{"content": "Resolve ticket B", "status": "completed"}]
                    },
                }
            ]
    # Both completions happened INSIDE this one /step call -- the shell
    # never saw the round-1 "no tool calls" attempt.
    assert calls["n"] == 2
    async with app_session(tenant) as db:  # type: ignore[operator]
        refetched = await db.get(m.AgentRun, run_id)
        assert refetched is not None
        # Each continuation round still counts as one ordinary step.
        assert refetched.context["steps"] == 2


@pytest.mark.asyncio
async def test_step_round_cap_stops_nudging_and_lets_the_run_end(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The isolated runtime's round cap must match engine.py's
    TODO_CONTINUATION_MAX_ROUNDS exactly and, once spent, let the run end
    anyway rather than nudging forever."""
    from oc8.agent.harness.stages.d_todo import TODO_CONTINUATION_MAX_ROUNDS
    from oc8.modelrouter.types import CompletionResult, Usage

    calls = {"n": 0}

    async def fake_complete(*args: object, **kw: object) -> CompletionResult:
        calls["n"] += 1
        return CompletionResult(
            text="I'm done.", tool_calls=[],
            usage=Usage(tokens_in=100, tokens_out=10), stop_reason="stop",
            provider="openrouter", model="z-ai/glm-5.3-flash",
        )

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(
            db, tenant, todos=[{"content": "Do the thing", "status": "pending"}]
        )
        agent_id, run_id = agent.id, run.id

    token = _agent_token(tenant, agent_id, run_id)
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["done"] is True
    # One initial finish attempt plus exactly TODO_CONTINUATION_MAX_ROUNDS
    # nudged retries -- never unbounded -- all within this one /step call.
    assert calls["n"] == 1 + TODO_CONTINUATION_MAX_ROUNDS
    # A run that gave up must not read like one that finished cleanly.
    assert "Do the thing" in body["text"]
    assert "still open" in body["text"]


@pytest.mark.asyncio
async def test_run_shell_records_the_locally_computed_result(app_session: object) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id

    code, body = await _post_tool(
        tenant, agent_id, run_id, "run_shell", {"command": "echo hi"},
        local_result={"stdout": "hi\n", "stderr": "", "exit_code": 0, "timed_out": False},
    )
    assert code == 200, body
    assert body["status"] == "ok"
    assert "hi" in body["output"]


@pytest.mark.asyncio
async def test_run_shell_without_a_local_result_is_an_error(app_session: object) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        agent, _task, run = await _plain_agent_run(db, tenant)
        agent_id, run_id = agent.id, run.id

    code, body = await _post_tool(tenant, agent_id, run_id, "run_shell", {"command": "echo hi"})
    assert code == 200, body
    assert body["output"].startswith("ERROR")


@pytest.mark.asyncio
async def test_step_withholds_run_shell_from_a_real_runtime_plugin(
    app_session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors test_step_withholds_write_output_file_from_a_real_runtime_plugin:
    a real runtime plugin has its own local shell inside its container --
    offering run_shell too would be a redundant, differently-shaped door to
    the same capability."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from oc8.main import create_app
    from oc8.modelrouter.types import CompletionResult, Usage

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:  # type: ignore[operator]
        lead, _mate, run = await _seed_lead_with_mate_and_skill(db, tenant)
        lead.runtime_ref = str(uuid.uuid4())
        lead_id, run_id = lead.id, run.id

    seen: dict[str, Any] = {}

    async def fake_complete(*args: object, **kw: object) -> CompletionResult:
        seen["tools"] = kw["tools"]
        return CompletionResult(
            text="fertig", tool_calls=[], usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop", provider="ollama", model="m",
        )

    monkeypatch.setattr(
        "oc8.api.v1.internal_agent.stream_completion_with_fallback", _as_stream(fake_complete)
    )
    token = _agent_token(tenant, lead_id, run_id)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/internal/agent/{run_id}/step",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text

    offered = {t.name for t in seen["tools"]}
    assert "run_shell" not in offered
