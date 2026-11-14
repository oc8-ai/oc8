"""§3 steps 1-2: the Copilot offers direct access, the person is asked."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.control_tools import offered_tools
from oc8.agent.harness.stages.b_authorize import authorize
from oc8.agent.harness.stages.b_direct_access import DirectAccessView
from oc8.authz.pdp import Effect
from oc8.chat.modes import MODES
from oc8.copilot.direct_access import direct_access_block, execute_direct_access_tool
from oc8.modelrouter import NeutralTool, ToolCall
from tests.conftest import AppSessionFactory
from tests.copilot.direct_access_helpers import (
    DirectOffice,
    approved_offer,
    chat_run,
    direct_office,
    grant,
)

COMMON: dict[str, Any] = {"assigned_skills": [], "active_skills": [], "mcp_tools": []}


def _copilot() -> m.Agent:
    return m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Copilot",
        is_tenant_assistant=True,
        is_team_lead=True,
        narrowing={},
        presentation={},
    )


def _names(tools: list[NeutralTool]) -> set[str]:
    return {t.name for t in tools}


def test_offered_only_on_the_web_door_to_a_present_member() -> None:
    web = offered_tools(_copilot(), copilot_door="web", offer_direct_access=True, **COMMON)
    assert "offer_direct_access" in _names(web)
    for door in ("followup", "telegram", None):
        assert "offer_direct_access" not in _names(
            offered_tools(_copilot(), copilot_door=door, offer_direct_access=True, **COMMON)
        )
    assert "offer_direct_access" not in _names(
        offered_tools(_copilot(), copilot_door="web", **COMMON)
    )


@pytest.mark.parametrize("mode_key", ["plan", "ask", "agent"])
def test_not_offered_in_a_mode_that_withholds_it(mode_key: str) -> None:
    names = _names(
        offered_tools(
            _copilot(),
            copilot_door="web",
            offer_direct_access=True,
            chat_mode=MODES[mode_key],
            **COMMON,
        )
    )
    assert "offer_direct_access" not in names


def test_the_pep_allows_the_offer_only_for_the_copilot_with_a_present_member() -> None:
    call = ToolCall(
        id="c", name="offer_direct_access", arguments={"connection": "things", "purpose": "x"}
    )
    kw: dict[str, Any] = {
        "frame": {},
        "tool_policies": {},
        "connection_key": None,
        "tool_scopes": None,
    }
    present = DirectAccessView(member_id=uuid.uuid4())
    assert authorize(_copilot(), call, direct_access=present, **kw).effect is Effect.ALLOW
    assert authorize(_copilot(), call, **kw).effect is Effect.DENY
    plain = _copilot()
    plain.is_tenant_assistant = False
    assert authorize(plain, call, direct_access=present, **kw).effect is Effect.DENY


async def _offer(db: AsyncSession, office: DirectOffice, run: m.AgentRun, **args: Any) -> Any:
    agent = await db.get(m.Agent, office.copilot_id)
    task = await db.get(m.Task, office.task_id)
    assert agent is not None and task is not None
    return await execute_direct_access_tool(
        db,
        tenant_id=office.tenant,
        agent=agent,
        task=task,
        tc=ToolCall(
            id="c",
            name="offer_direct_access",
            arguments={"connection": "things", "purpose": "find the quotation", **args},
        ),
        run_id=run.id,
    )


async def _offers(db: AsyncSession) -> list[m.ApprovalRequest]:
    return list(
        (
            await db.execute(
                select(m.ApprovalRequest).where(m.ApprovalRequest.action_type == "direct_access")
            )
        ).scalars()
    )


@pytest.mark.asyncio
async def test_an_offer_asks_the_person_and_parks_the_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        run = await chat_run(db, office)
        outcome = await _offer(db, office, run)
        assert outcome.suspend == "waiting_for_approval"
        (offer,) = await _offers(db)
        info = offer.payload["direct_access"]
        assert offer.task_id == office.task_id
        assert offer.payload["run_id"] == str(run.id)
        assert info["member_id"] == str(office.member_id)
        assert info["session_id"] == str(office.session_id)
        assert info["tool_key"] == "things" and info["decider"] == "member"
        assert info["account"] == "organisation"
        assert "Anna" in offer.title
        assert offer.payload["i18n"]["de"]["title"]


@pytest.mark.asyncio
async def test_a_second_offer_while_one_waits_raises_nothing(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        run = await chat_run(db, office)
        await _offer(db, office, run)
        again = await _offer(db, office, run)
        assert again.suspend == "waiting_for_approval"
        assert len(await _offers(db)) == 1


@pytest.mark.asyncio
async def test_a_connection_the_person_may_not_use_is_refused(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant, seated=False)
        run = await chat_run(db, office)
        outcome = await _offer(db, office, run)
        assert outcome.output.startswith("ERROR") and outcome.suspend is None
        assert await _offers(db) == []


@pytest.mark.asyncio
async def test_an_existing_grant_needs_no_new_offer(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await grant(db, office)
        run = await chat_run(db, office)
        outcome = await _offer(db, office, run)
        assert outcome.suspend is None and "already" in outcome.output
        assert len(await _offers(db)) == 1  # only the fixture's own approved one


@pytest.mark.asyncio
async def test_a_declined_offer_is_not_made_again_in_the_same_run(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        run = await chat_run(
            db,
            office,
            extra={"resolved_direct_access": {"tool_key": "things", "decision": "reject"}},
        )
        outcome = await _offer(db, office, run)
        assert outcome.output.startswith("ERROR") and await _offers(db) == []


@pytest.mark.asyncio
async def test_nobody_present_gets_no_offer(app_session: AppSessionFactory, redis_url: str) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        run = await chat_run(db, office, on_behalf=False)
        outcome = await _offer(db, office, run)
        assert outcome.output.startswith("ERROR") and await _offers(db) == []


@pytest.mark.asyncio
async def test_the_prompt_lists_what_the_person_may_use(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        agent = await db.get(m.Agent, office.copilot_id)
        assert agent is not None
        block = await direct_access_block(db, run=await chat_run(db, office), agent=agent)
        assert block is not None and "offer_direct_access" in block and "things" in block
        assert (
            await direct_access_block(
                db, run=await chat_run(db, office, on_behalf=False), agent=agent
            )
            is None
        )


@pytest.mark.asyncio
async def test_a_second_run_does_not_park_on_another_runs_offer(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        first = await chat_run(db, office)
        await _offer(db, office, first)
        second = await chat_run(db, office)
        again = await _offer(db, office, second)
        assert again.suspend is None and "already" in again.output
        assert len(await _offers(db)) == 1


@pytest.mark.asyncio
async def test_the_offer_is_titled_for_the_person_and_has_a_german_title(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _offer(db, office, await chat_run(db, office))
        (offer,) = await _offers(db)
        assert offer.title.startswith("Copilot on behalf of Anna: ")
        assert offer.payload["i18n"]["de"]["title"].startswith("Copilot im Auftrag von Anna")
        assert offer.expires_at is None


@pytest.mark.asyncio
async def test_the_copilots_own_narrowing_is_a_ceiling_for_the_offer(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        agent = await db.get(m.Agent, office.copilot_id)
        assert agent is not None
        agent.narrowing = {"tools": {"things": {"read": False}}}
        await db.flush()
        outcome = await _offer(db, office, await chat_run(db, office))
        assert outcome.output.startswith("ERROR") and await _offers(db) == []
        block = await direct_access_block(db, run=await chat_run(db, office), agent=agent)
        assert block is None or "things |" not in block


@pytest.mark.asyncio
async def test_the_prompt_marks_a_connection_the_person_declined(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await approved_offer(db, office, status="rejected")
        agent = await db.get(m.Agent, office.copilot_id)
        assert agent is not None
        block = await direct_access_block(db, run=await chat_run(db, office), agent=agent)
        assert block is not None
        assert "declined" in block and "offer again only if the person asks" in block


READ_ONLY_LIST: dict[str, Any] = {
    "enabled": True,
    "read": True,
    "modify": True,
    "only": ["search_records"],
}


@pytest.mark.asyncio
async def test_a_frame_with_an_only_list_still_offers_its_read_tool(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant, policy=READ_ONLY_LIST)
        agent = await db.get(m.Agent, office.copilot_id)
        assert agent is not None
        block = await direct_access_block(db, run=await chat_run(db, office), agent=agent)
        assert block is not None and "things |" in block
        outcome = await _offer(db, office, await chat_run(db, office))
        assert outcome.suspend == "waiting_for_approval" and len(await _offers(db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "narrowing",
    [
        {"tools": {"things": {"only": ["update_record"]}}},
        {"tools": {"things": {"only": ["search_records"], "read": False}}},
    ],
    ids=["only-without-a-read-tool", "read-denied"],
)
async def test_a_narrowing_that_leaves_no_read_refuses_the_offer(
    app_session: AppSessionFactory, redis_url: str, narrowing: dict[str, Any]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant, policy=READ_ONLY_LIST)
        agent = await db.get(m.Agent, office.copilot_id)
        assert agent is not None
        agent.narrowing = narrowing
        await db.flush()
        outcome = await _offer(db, office, await chat_run(db, office))
        assert outcome.output.startswith("ERROR") and await _offers(db) == []


class _SpyBus:
    def __init__(self) -> None:
        self.tenant_wide: list[str] = []
        self.member: list[tuple[list[uuid.UUID], str, dict[str, Any]]] = []

    async def publish_event(
        self, tenant_id: uuid.UUID, type_: str, data: dict[str, Any], *, source: str
    ) -> None:
        self.tenant_wide.append(type_)

    async def publish_member_event(
        self,
        tenant_id: uuid.UUID,
        member_ids: Any,
        type_: str,
        data: dict[str, Any],
        *,
        source: str,
    ) -> None:
        self.member.append((list(member_ids), type_, data))


@pytest.mark.asyncio
async def test_an_offer_is_announced_to_its_member_only(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _SpyBus()
    monkeypatch.setattr("oc8.realtime.bus.get_event_bus", lambda: spy)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _offer(db, office, await chat_run(db, office))
    assert "approval.created" not in spy.tenant_wide
    ((audience, type_, data),) = spy.member
    assert audience == [office.member_id] and type_ == "approval.created"
    assert data["title"].startswith("Copilot on behalf of Anna")


@pytest.mark.asyncio
async def test_a_declined_key_later_allowed_is_not_declined(
    app_session: AppSessionFactory,
) -> None:
    import datetime as dt

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        now = dt.datetime.now(dt.UTC)
        old = await approved_offer(db, office, status="rejected")
        old.decided_at = now - dt.timedelta(minutes=5)
        new = await approved_offer(db, office, status="approved")
        new.decided_at = now
        await db.flush()
        agent = await db.get(m.Agent, office.copilot_id)
        assert agent is not None
        block = await direct_access_block(db, run=await chat_run(db, office), agent=agent)
        assert block is not None and "declined" not in block
