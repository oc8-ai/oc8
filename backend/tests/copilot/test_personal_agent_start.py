"""Only the owner starts a personal agent, on every path that can start one.

`agents.repo.agent_runnable_in` is the one answer to "may this caller start
this agent" -- the run route, the boards' `may_run`, the Copilot's roster and
delegation, and the agent match behind direct access all ask it. Its start
reach now carries the personal departments the caller does not own, so an
`org_admin` behind the Copilot cannot start a member's personal agent and get
its result back, while the owner still can and a standard agent is unchanged.

Both people carry an `org_admin` token: a plain `member` holds no `run:start`
on this branch (PR 3), so the owner's seat is what tells the two apart.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.agent.control_tools import ControlOutcome, execute_control_tool
from oc8.agent.harness.caps import ModelCaps
from oc8.agent.preamble import build_run_preamble
from oc8.authz.pdp import Decision, Effect
from oc8.authz.permissions import ORG_ADMIN, SEAT_APPROVER
from oc8.copilot import agent_match
from oc8.copilot.agent_match import matching_agents, member_may_run_agent
from oc8.modelrouter import ToolCall
from tests.api.test_personal_agent_content import _headers, _http
from tests.boards.test_mission import SPEC
from tests.conftest import AppSessionFactory
from tests.copilot.direct_access_helpers import READ_WRITE

pytestmark = pytest.mark.asyncio

_REQUEST = "answer the landlord"


@dataclass
class _Office:
    tenant: uuid.UUID
    owner: m.OrgMember
    admin: m.OrgMember
    assistant: m.Agent
    pia: m.Agent
    nora: m.Agent


async def _office(db: AsyncSession, tenant: uuid.UUID) -> _Office:
    """`owner`'s personal department with Pia, a standard "Vertrieb" with Nora
    (both carrying the `things` tool), an admin, and the tenant Copilot."""
    people = {}
    for subject in ("owner", "admin"):
        member = m.OrgMember(
            tenant_id=tenant,
            subject=subject,
            subject_uuid=uuid.uuid4(),
            display_name=subject.title(),
        )
        db.add(member)
        people[subject] = member
    frame = {"tools": {"things": dict(READ_WRITE)}}
    vertrieb = m.Department(tenant_id=tenant, name="Vertrieb", frame=frame)
    db.add(vertrieb)
    await db.flush()
    personal = m.Department(
        tenant_id=tenant,
        name="Owner",
        frame={**frame, "closed": True},
        kind="personal",
        owner_member_id=people["owner"].id,
    )
    db.add(personal)
    await db.flush()
    db.add(
        m.OrgMemberDepartment(
            tenant_id=tenant,
            member_id=people["owner"].id,
            department_id=personal.id,
            seat_role=SEAT_APPROVER,
            agent_manage=True,
        )
    )
    pia = m.Agent(
        tenant_id=tenant,
        department_id=personal.id,
        name="Pia",
        mission="Answers the landlord",
        status="idle",
        definition={},
        presentation={},
    )
    nora = m.Agent(
        tenant_id=tenant,
        department_id=vertrieb.id,
        name="Nora",
        mission="Answers the landlord",
        status="idle",
        definition={},
        presentation={},
    )
    db.add_all([pia, nora])
    await db.flush()
    assistant = await get_or_create_assistant(db, tenant_id=tenant)
    return _Office(tenant, people["owner"], people["admin"], assistant, pia, nora)


async def _turn(db: AsyncSession, o: _Office, person: m.OrgMember) -> tuple[m.Task, m.AgentRun]:
    """A live web Copilot turn `person` wrote, with their org_admin token."""
    task = m.Task(
        tenant_id=o.tenant,
        department_id=o.assistant.department_id,
        assigned_agent_id=o.assistant.id,
        title="chat",
        state="in_progress",
    )
    db.add(task)
    await db.flush()
    session = m.ChatSession(
        tenant_id=o.tenant, agent_id=o.assistant.id, member_id=person.id, task_id=task.id
    )
    db.add(session)
    await db.flush()
    run = m.AgentRun(
        tenant_id=o.tenant,
        agent_id=o.assistant.id,
        task_id=task.id,
        source="chat",
        state="running",
        context={
            "door": "web",
            "on_behalf_of": str(person.id),
            "chat_session_id": str(session.id),
            "operator_role": ORG_ADMIN,
            "originating_operator": person.subject,
        },
    )
    db.add(run)
    await db.flush()
    return task, run


async def _delegate(
    db: AsyncSession, o: _Office, person: m.OrgMember, target: m.Agent
) -> ControlOutcome:
    task, run = await _turn(db, o, person)
    outcome = await execute_control_tool(
        db,
        tenant_id=o.tenant,
        agent=o.assistant,
        task=task,
        tc=ToolCall(
            id="c1",
            name="delegate_task",
            arguments={"agent_id": str(target.id), "task_text": "Read my mail"},
        ),
        decision=Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
        run_id=run.id,
    )
    assert outcome is not None
    return outcome


async def _delegations_to(db: AsyncSession, agent: m.Agent) -> int:
    rows = await db.execute(
        select(m.AgentRun).where(m.AgentRun.agent_id == agent.id, m.AgentRun.source == "delegation")
    )
    return len(rows.scalars().all())


# ------------------------------------------------------------ delegate_task


async def test_an_admins_copilot_cannot_delegate_to_a_personal_agent(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        o = await _office(db, tenant)
        outcome = await _delegate(db, o, o.admin, o.pia)
        assert outcome.pending_run is None
        assert outcome.output == "refused: the person may not run Pia"
        assert await _delegations_to(db, o.pia) == 0


async def test_the_owners_copilot_delegates_to_their_own_agent(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        o = await _office(db, tenant)
        outcome = await _delegate(db, o, o.owner, o.pia)
        assert outcome.pending_run is not None, outcome.output
        assert await _delegations_to(db, o.pia) == 1


async def test_an_admins_copilot_still_delegates_to_a_standard_agent(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        o = await _office(db, tenant)
        outcome = await _delegate(db, o, o.admin, o.nora)
        assert outcome.pending_run is not None, outcome.output


# ---------------------------------------------------------------- roster


async def _roster(db: AsyncSession, o: _Office, person: m.OrgMember | None) -> str:
    kw: dict[str, Any] = {}
    if person is not None:
        task, run = await _turn(db, o, person)
        kw = {"task": task, "run_id": run.id}
    pre = await build_run_preamble(
        db,
        agent=o.assistant,
        tenant_id=o.tenant,
        task_text="hi",
        frame={},
        model_locality="cloud",
        caps=ModelCaps(),
        max_steps=40,
        **kw,
    )
    return "\n".join(msg.content for msg in pre.messages if isinstance(msg.content, str))


async def test_the_roster_omits_a_personal_agent_for_an_admin(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        o = await _office(db, tenant)
        admins = await _roster(db, o, o.admin)
        owners = await _roster(db, o, o.owner)
        nobodys = await _roster(db, o, None)
    assert str(o.nora.id) in admins
    assert str(o.pia.id) not in admins
    assert str(o.pia.id) in owners
    assert str(o.nora.id) in owners
    # A run with nobody behind it delegates across no department at all, so
    # it is not told about an agent it could never reach.
    assert str(o.pia.id) not in nobodys
    assert str(o.nora.id) in nobodys


# ---------------------------------------------- direct access's agent match


class _Router:
    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        return [1.0, 0.0]


async def test_direct_access_never_offers_a_personal_agent_to_an_admin(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`matching_agents` decides which agents an offer names, and
    `member_may_run_agent` is checked again when the person picks one."""
    monkeypatch.setattr("oc8.copilot.agent_match.get_model_router", lambda: _Router())
    agent_match.clear_embedding_cache()
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        o = await _office(db, tenant)
        for person in (o.admin, o.owner):
            found = await matching_agents(
                db,
                member_id=person.id,
                tool_key="things",
                request=_REQUEST,
                copilot=o.assistant,
                token_role=ORG_ADMIN,
            )
            names = {f.name for f in found}
            assert "Nora" in names
            assert ("Pia" in names) is (person is o.owner)

        def may(person: m.OrgMember, agent: m.Agent) -> Any:
            return member_may_run_agent(db, member_id=person.id, agent=agent, token_role=ORG_ADMIN)

        assert await may(o.admin, o.pia) is False
        assert await may(o.owner, o.pia) is True
        assert await may(o.admin, o.nora) is True
    agent_match.clear_embedding_cache()


# ---------------------------------------------------------------- boards


async def test_a_personal_agents_board_is_not_runnable_for_an_admin(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        o = await _office(db, tenant)
        for agent in (o.pia, o.nora):
            db.add(
                m.DataBoard(
                    tenant_id=tenant,
                    department_id=agent.department_id,
                    agent_id=agent.id,
                    title=f"{agent.name} board",
                    spec=copy.deepcopy(SPEC),
                )
            )
        await db.flush()

    async def may_run(subject: str) -> dict[str, bool]:
        async with _http() as http:
            r = await http.get("/api/v1/boards", headers=_headers(tenant, subject, ORG_ADMIN))
        assert r.status_code == 200, r.text
        return {b["title"]: b["mayRun"] for b in r.json()}

    admin = await may_run("admin")
    owner = await may_run("owner")
    assert admin.get("Pia board", False) is False
    assert admin["Nora board"] is True
    assert owner["Pia board"] is True
    assert owner["Nora board"] is True
