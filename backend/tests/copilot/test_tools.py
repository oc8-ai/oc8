from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.control_tools import ASK_USER, offered_tools
from oc8.agent.engine import _authorize
from oc8.authz.pdp import Effect, ToolPolicy
from oc8.chat.modes import MODES
from oc8.copilot.tools import COPILOT_TOOLS, execute_copilot_tool
from oc8.modelrouter import NeutralTool, ToolCall
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import Seat


def _names(tools: Iterable[NeutralTool]) -> set[str]:
    return {t.name for t in tools}


def _copilot() -> m.Agent:
    return m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Copilot",
        is_tenant_assistant=True,
        is_team_lead=True,
    )


def test_ask_user_offered_per_door() -> None:
    a = _copilot()
    common: dict[str, Any] = {"assigned_skills": [], "active_skills": [], "mcp_tools": []}
    assert ASK_USER.name in _names(offered_tools(a, copilot_door="web", **common))
    assert ASK_USER.name in _names(offered_tools(a, copilot_door="followup", **common))
    assert ASK_USER.name not in _names(offered_tools(a, copilot_door="telegram", **common))
    assert ASK_USER.name not in _names(offered_tools(a, copilot_door=None, **common))


def test_copilot_tools_only_for_copilot() -> None:
    common: dict[str, Any] = {"assigned_skills": [], "active_skills": [], "mcp_tools": []}
    mine = {t.name for t in COPILOT_TOOLS}
    assert mine <= _names(offered_tools(_copilot(), copilot_door="web", **common))
    plain = m.Agent(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), department_id=uuid.uuid4(), name="Lena"
    )
    assert not (mine & _names(offered_tools(plain, **common)))


def test_plan_withholds_the_dot_tools_and_authorize_agrees() -> None:
    a = _copilot()
    common: dict[str, Any] = {"assigned_skills": [], "active_skills": [], "mcp_tools": []}
    plan = _names(offered_tools(a, copilot_door="web", chat_mode=MODES["plan"], **common))
    assert not ({t.name for t in COPILOT_TOOLS} & plan)

    def decide(mode_key: str | None) -> Effect:
        return _authorize(
            a,
            ToolCall(id="c", name="responsibility_open", arguments={"title": "t", "goal": "g"}),
            frame={"tools": {}},
            tool_policies={"x": ToolPolicy(enabled=True, read=True, modify=True)},
            connection_key=None,
            tool_scopes=None,
            chat_mode=MODES[mode_key] if mode_key else None,
        ).effect

    assert decide(None) is Effect.ALLOW
    assert decide("plan") is Effect.DENY


async def _chat_run(
    db: AsyncSession, tenant: uuid.UUID, cop: m.Agent, seat: Seat, **ctx: Any
) -> m.AgentRun:
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=cop.id,
        source="chat",
        state="running",
        task_id=seat.task_id,
        context={"chat_session_id": str(seat.session_id), **ctx},
    )
    db.add(run)
    await db.flush()
    return run


async def test_open_then_schedule_renders_cards(app_session: AppSessionFactory) -> None:
    import datetime as dt

    from oc8.agent.assistant import get_or_create_assistant
    from tests.copilot.helpers import copilot_seat

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        lisa = await copilot_seat(db, tenant, "lisa@example.com")
        max_ = await copilot_seat(db, tenant, "max@example.com")
        lisa_task = await db.get(m.Task, lisa.task_id)
        max_task = await db.get(m.Task, max_.task_id)
        assert lisa_task is not None and max_task is not None
        run = await _chat_run(db, tenant, cop, lisa, door="web")

        opened = await execute_copilot_tool(
            db,
            tenant_id=tenant,
            agent=cop,
            task=lisa_task,
            run_id=run.id,
            tc=ToolCall(
                id="1",
                name="responsibility_open",
                arguments={"title": "Offsite", "goal": "Keep it on track"},
            ),
        )
        assert opened is not None and opened.rendered_component is not None
        assert opened.rendered_component["component_key"] == "responsibility_card"
        r = (await db.execute(select(m.Responsibility))).scalar_one()
        assert r.member_id == lisa.member_id and r.origin_channel is None

        ends = (dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=20)).isoformat()
        saved = await execute_copilot_tool(
            db,
            tenant_id=tenant,
            agent=cop,
            task=lisa_task,
            run_id=run.id,
            tc=ToolCall(
                id="2",
                name="schedule_followup",
                arguments={
                    "responsibility_id": str(r.id),
                    "kind": "cron",
                    "cron_expression": "0 9 * * 1-5",
                    "timezone": "Europe/Berlin",
                    "ends_at": ends,
                    "prompt": "check",
                },
            ),
        )
        assert saved is not None and saved.rendered_component is not None
        assert saved.rendered_component["component_key"] == "followup_card"
        props = saved.rendered_component["props"]
        assert props["responsibilityTitle"] == "Offsite"
        assert props["when"].endswith(("+01:00", "+02:00"))  # the trigger's zone, not UTC
        triggers = (
            (await db.execute(select(m.Trigger).where(m.Trigger.enabled.is_(True)))).scalars().all()
        )
        assert len(triggers) == 1

        no_zone = await execute_copilot_tool(
            db,
            tenant_id=tenant,
            agent=cop,
            task=lisa_task,
            run_id=run.id,
            tc=ToolCall(
                id="3",
                name="schedule_followup",
                arguments={
                    "responsibility_id": str(r.id),
                    "kind": "once",
                    "run_at": "2030-01-01T09:00:00+01:00",
                    "prompt": "x",
                },
            ),
        )
        assert no_zone is not None and no_zone.output.startswith("ERROR:")
        assert "time zone" in no_zone.output

        foreign = await execute_copilot_tool(
            db,
            tenant_id=tenant,
            agent=cop,
            task=max_task,
            run_id=None,
            tc=ToolCall(
                id="4",
                name="responsibility_update",
                arguments={"responsibility_id": str(r.id), "next_step": "steal"},
            ),
        )
        assert foreign is not None and foreign.output == "ERROR: responsibility not found"


async def test_messenger_turn_records_origin_channel(app_session: AppSessionFactory) -> None:
    from oc8.agent.assistant import get_or_create_assistant
    from tests.copilot.helpers import copilot_seat

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cop = await get_or_create_assistant(db, tenant_id=tenant)
        lisa = await copilot_seat(db, tenant, "lisa@example.com")
        task = await db.get(m.Task, lisa.task_id)
        assert task is not None
        run = await _chat_run(
            db,
            tenant,
            cop,
            lisa,
            door="telegram",
            chat_channel="telegram",
            chat_channel_external_id="4711",
        )
        await execute_copilot_tool(
            db,
            tenant_id=tenant,
            agent=cop,
            task=task,
            run_id=run.id,
            tc=ToolCall(id="1", name="responsibility_open", arguments={"title": "t", "goal": "g"}),
        )
        r = (await db.execute(select(m.Responsibility))).scalar_one()
        assert r.origin_channel == "telegram"
