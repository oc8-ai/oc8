"""An agent in a container can reach its knowledge base.

The in-process engine retrieves knowledge once, at the start, from the task
text. A container-run agent cannot work that way: its task text is the trigger
("check the ticket inbox"), and what it needs to look up only becomes clear
once it has read the ticket. So the nanoclaw runtime retrieved nothing at all --
a knowledge base granted to such an agent was, in practice, unreachable.

Made a tool for the same reason skills are one: the agent asks when it knows
what to ask.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.control_tools import SEARCH_KNOWLEDGE, SKIPPED_LOCAL_LINE, execute_control_tool
from oc8.authz.pdp import Decision, Effect
from oc8.modelrouter import ToolCall
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _agent_and_task(db: Any, tenant: uuid.UUID) -> tuple[m.Agent, m.Task]:
    agent = m.Agent(tenant_id=tenant, department_id=uuid.uuid4(), name="Sina")
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=agent.department_id,
        assigned_agent_id=agent.id,
        title="Ticket-Eingang",
        state="in_progress",
    )
    db.add(task)
    await db.flush()
    return agent, task


async def _run(db: Any, tenant: uuid.UUID, query: str, monkeypatch: Any, answer: str) -> Any:
    agent, task = await _agent_and_task(db, tenant)

    async def fake_retrieve(*a: Any, **kw: Any) -> tuple[str, bool]:
        fake_retrieve.seen = kw  # type: ignore[attr-defined]
        return answer, False

    monkeypatch.setattr("oc8.agent.control_tools.retrieve_kb_context", fake_retrieve)
    outcome = await execute_control_tool(
        db,
        tenant_id=tenant,
        agent=agent,
        task=task,
        tc=ToolCall(id="c1", name=SEARCH_KNOWLEDGE.name, arguments={"query": query}),
        decision=Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
    )
    return outcome, fake_retrieve


async def test_the_agent_gets_what_the_knowledge_base_holds(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        outcome, retrieve = await _run(
            db, tenant, "Erstattungsfrist", monkeypatch, "Erstattungen: 14 Tage."
        )
    assert outcome is not None
    assert "14 Tage" in outcome.output
    # The agent's OWN question is what gets searched -- not the task text, which
    # for a scheduled agent says only "check the inbox".
    assert retrieve.seen["query_text"] == "Erstattungsfrist"


async def test_a_base_skipped_for_needing_a_local_embedding_is_said_but_never_named(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The member is told once, in the tool result the chat shows under the
    answer's steps (E2E H), whether or not anything was found. The result
    reaches the model, which may be a cloud one: a skipped base -- here a
    restricted one -- is never named in it."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, task = await _agent_and_task(db, tenant)

        async def fake_retrieve(*a: Any, **kw: Any) -> tuple[str, bool]:
            kw["skipped_local"].extend(["Geheime Gehaltsliste", "Sales KB"])
            return "Erstattungen: 14 Tage.", False

        monkeypatch.setattr("oc8.agent.control_tools.retrieve_kb_context", fake_retrieve)
        outcome = await execute_control_tool(
            db,
            tenant_id=tenant,
            agent=agent,
            task=task,
            tc=ToolCall(id="c1", name=SEARCH_KNOWLEDGE.name, arguments={"query": "Frist"}),
            decision=Decision(Effect.ALLOW),
            assigned_skills=[],
            active_skills=[],
            mcp_conn=None,
            originating_operator=None,
        )
    assert outcome is not None
    assert "14 Tage" in outcome.output
    assert "Geheime Gehaltsliste" not in outcome.output
    assert "Sales KB" not in outcome.output
    assert outcome.output.count(SKIPPED_LOCAL_LINE) == 1
    # First: a step's stored result is cut at 300 characters.
    assert outcome.output.startswith(SKIPPED_LOCAL_LINE)


async def test_nothing_found_is_said_plainly(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty answer must read as "we have nothing on this", or the model
    fills the silence with something it invented."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        outcome, _r = await _run(db, tenant, "Raketenantrieb", monkeypatch, "")
    assert outcome is not None
    assert "nichts" in outcome.output.lower() or "no " in outcome.output.lower()


async def test_an_empty_query_is_refused(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        outcome, _r = await _run(db, tenant, "   ", monkeypatch, "irgendwas")
    assert outcome is not None
    assert outcome.output.startswith("ERROR")


async def test_the_model_locality_is_passed_so_restricted_material_stays_local(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrieval filters restricted content by where the model runs. Handing a
    cloud-model agent restricted text here would route it straight out through
    the LLM gateway, which cannot tell what it is carrying."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        _outcome, retrieve = await _run(db, tenant, "Preise", monkeypatch, "x")
    assert retrieve.seen["model_locality"] in ("cloud", "local")


async def test_a_lookup_leaves_a_trail(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Did the agent consult the handbook, or did it guess?" has to be
    answerable afterwards.

    It was not, and I got it wrong myself: with no audit event and no feed line,
    I counted zero lookups and reported that the agent had ignored the knowledge
    base -- while its answer quoted that knowledge base word for word. A tool
    whose use leaves no trace cannot be supervised, and for the one tool that
    decides whether a customer is told a fact or a guess, that is the wrong
    property to have.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await _run(db, tenant, "Garantiedauer", monkeypatch, "5 Jahre auf die Konstruktion.")

        events = (await db.execute(m.ActivityEvent.__table__.select())).fetchall()
        assert any("Garantiedauer" in (e.message or "") for e in events), "no feed line"

        audit = (await db.execute(m.AuditEvent.__table__.select())).fetchall()
        assert any(a.action == "knowledge.searched" for a in audit), "no audit entry"


async def test_restricted_text_fetched_mid_run_keeps_the_rest_of_the_run_off_cloud_models(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every completion carries `contains_restricted`, which makes the router
    refuse a cloud model or a cloud fallback. It was fixed at the start of the
    run, so restricted text the agent looked up later rode on as unrestricted."""
    from oc8.modelrouter import CompletionResult, Usage, chunk_from_result

    class _Router:
        def __init__(self) -> None:
            self.flags: list[bool] = []

        async def complete(self, req: Any) -> CompletionResult:
            self.flags.append(bool(req.contains_restricted))
            if len(self.flags) == 1:
                return CompletionResult(
                    text="",
                    tool_calls=[
                        ToolCall(id="c1", name=SEARCH_KNOWLEDGE.name, arguments={"query": "Gehalt"})
                    ],
                    usage=Usage(tokens_in=1, tokens_out=1),
                    stop_reason="tool_use",
                    provider=req.provider,
                    model=req.model,
                )
            return CompletionResult(
                text="fertig",
                tool_calls=[],
                usage=Usage(tokens_in=1, tokens_out=1),
                stop_reason="stop",
                provider=req.provider,
                model=req.model,
            )

        async def stream(self, req: Any) -> Any:
            yield chunk_from_result(await self.complete(req))

    from oc8.agent.engine import run_agent

    router = _Router()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)

    async def nothing_up_front(*a: Any, **kw: Any) -> tuple[str, bool]:
        return "", False

    async def restricted_hit(*a: Any, **kw: Any) -> tuple[str, bool]:
        return "Gehaltstabelle", True

    monkeypatch.setattr("oc8.agent.preamble.retrieve_kb_context", nothing_up_front)
    monkeypatch.setattr("oc8.agent.control_tools.retrieve_kb_context", restricted_hit)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, _task = await _agent_and_task(db, tenant)
        kb = m.KnowledgeBase(tenant_id=tenant, name="HR", embedding_model="nomic-embed-text")
        db.add(kb)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        await db.flush()
        await run_agent(db, agent=agent, task_text="Gehälter prüfen", tenant_id=tenant)

    assert router.flags[0] is False, "nothing restricted before the lookup"
    assert router.flags[1:] and all(router.flags[1:]), "everything after it is restricted"
