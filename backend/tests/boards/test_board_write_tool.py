"""The board_write control tool: offering, dispatch, refusals and the PEP."""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.control_tools import ControlOutcome, execute_control_tool, offered_tools
from oc8.agent.engine import _authorize
from oc8.agent.harness.stages.b_risk_tier import classify_tier
from oc8.authz.pdp import Decision, Effect
from oc8.chat.modes import PLAN
from oc8.modelrouter import ToolCall
from tests.audit.helpers import flush_audit
from tests.boards.test_mission import SPEC
from tests.conftest import AppSessionFactory


def _names(tools: list[Any]) -> set[str]:
    return {t.name for t in tools}


def _agent(*, assistant: bool = False) -> m.Agent:
    return m.Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        name="Nora",
        narrowing={},
        definition={},
        presentation={},
        is_tenant_assistant=assistant,
    )


def _offer(agent: m.Agent, **kw: Any) -> list[Any]:
    return offered_tools(agent, assigned_skills=[], active_skills=[], mcp_tools=[], **kw)


def test_offered_only_to_a_producing_agent() -> None:
    assert "board_write" not in _names(_offer(_agent()))
    assert "board_write" in _names(_offer(_agent(), produces_board=True))
    assert "board_write" not in _names(_offer(_agent(assistant=True), produces_board=True))


def test_withheld_in_plan_mode() -> None:
    assert "board_write" not in _names(_offer(_agent(), produces_board=True, chat_mode=PLAN))


async def _seed(
    db: AsyncSession, tenant: uuid.UUID
) -> tuple[m.Agent, m.DataBoard, m.Task, m.AgentRun]:
    dept = m.Department(tenant_id=tenant, name="Finance", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Reporter",
        status="running",
        definition={},
        presentation={},
        narrowing={},
    )
    db.add(agent)
    await db.flush()
    board = m.DataBoard(
        tenant_id=tenant,
        department_id=dept.id,
        agent_id=agent.id,
        title="Revenue",
        spec=copy.deepcopy(SPEC),
    )
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=agent.id,
        title="Report",
        state="in_progress",
        delegation_depth=0,
    )
    run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running")
    db.add_all([board, task, run])
    # The tool writes in its own transaction, so what it reads must be committed.
    await db.commit()
    await db.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
    return agent, board, task, run


async def _call(
    db: AsyncSession,
    tenant: uuid.UUID,
    seeded: tuple[m.Agent, m.DataBoard, m.Task, m.AgentRun],
    args: dict[str, Any],
    decision: Decision | None = None,
) -> ControlOutcome:
    agent, _board, task, run = seeded
    outcome = await execute_control_tool(
        db,
        tenant_id=tenant,
        agent=agent,
        task=task,
        tc=ToolCall(id="c1", name="board_write", arguments=args),
        decision=decision or Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
        run_id=run.id,
    )
    assert outcome is not None
    return outcome


async def _count(db: AsyncSession, model: Any) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def test_writes_a_batch_and_audits(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seeded = await _seed(db, tenant)
        board = seeded[1]
        outcome = await _call(
            db,
            tenant,
            seeded,
            {
                "board": str(board.id),
                "dataset": "sales",
                "as_of": "2026-03-01",
                "rows": [{"amount": 1, "region": "n"}, {"amount": 2, "region": "s"}],
            },
        )
        assert outcome.output.startswith("Recorded 2 row"), outcome.output
        batch = (await db.execute(select(m.DataBoardBatch))).scalar_one()
        assert batch.run_id == seeded[3].id
        audit = (
            await db.execute(select(m.AuditEvent).where(m.AuditEvent.action == "board.written"))
        ).scalar_one()
        assert audit.resource["batch_id"] == str(batch.id)
        assert audit.resource["board_id"] == str(board.id)
        assert audit.resource["dataset"] == "sales"
        assert audit.resource["rows"] == 2


async def test_values_is_a_single_record(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seeded = await _seed(db, tenant)
        outcome = await _call(
            db,
            tenant,
            seeded,
            {
                "board": str(seeded[1].id),
                "dataset": "sales",
                "as_of": "2026-03-01",
                "values": {"amount": 5},
            },
        )
        assert outcome.output.startswith("Recorded 1 row"), outcome.output
        assert await _count(db, m.DataBoardRecord) == 1


@pytest.mark.parametrize("extra", [{}, {"values": {"amount": 1}, "rows": [{"amount": 2}]}])
async def test_values_and_rows_are_exclusive(
    app_session: AppSessionFactory, extra: dict[str, Any]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seeded = await _seed(db, tenant)
        outcome = await _call(
            db,
            tenant,
            seeded,
            {"board": str(seeded[1].id), "dataset": "sales", "as_of": "2026-03-01", **extra},
        )
        assert outcome.output.startswith("ERROR:"), outcome.output
        assert await _count(db, m.DataBoardBatch) == 0


async def test_refusal_is_a_correctable_error(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seeded = await _seed(db, tenant)
        outcome = await _call(
            db,
            tenant,
            seeded,
            {
                "board": str(seeded[1].id),
                "dataset": "sales",
                "as_of": "2026-03-01",
                "values": {"amount": "12"},
            },
        )
        assert outcome.output.startswith("ERROR:") and "amount" in outcome.output
        assert "12" not in outcome.output.replace("2026-03-01", "")
        await flush_audit(db)
        assert await _count(db, m.DataBoardBatch) == 0
        assert await _count(db, m.AuditEvent) == 0


async def test_a_long_note_is_refused_value_free(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seeded = await _seed(db, tenant)
        outcome = await _call(
            db,
            tenant,
            seeded,
            {
                "board": str(seeded[1].id),
                "dataset": "sales",
                "as_of": "2026-03-01",
                "values": {"amount": 1},
                "note": "SECRET" * 400,
            },
        )
        assert outcome.output.startswith("ERROR:") and "note" in outcome.output
        assert "SECRET" not in outcome.output
        assert await _count(db, m.DataBoardBatch) == 0


async def test_a_retired_agent_cannot_write(app_session: AppSessionFactory) -> None:
    import datetime as dt

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seeded = await _seed(db, tenant)
        seeded[0].deleted_at = dt.datetime.now(dt.UTC)
        await db.flush()
        outcome = await _call(
            db,
            tenant,
            seeded,
            {
                "board": str(seeded[1].id),
                "dataset": "sales",
                "as_of": "2026-03-01",
                "values": {"amount": 1},
            },
        )
        assert outcome.output.startswith("ERROR:") and "retired" in outcome.output
        assert await _count(db, m.DataBoardBatch) == 0


async def test_a_bad_board_id_and_a_denial_are_errors(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seeded = await _seed(db, tenant)
        args = {"dataset": "sales", "as_of": "2026-03-01", "values": {"amount": 1}}
        bad = await _call(db, tenant, seeded, {**args, "board": "nope"})
        assert bad.output.startswith("ERROR:")
        denied = await _call(
            db,
            tenant,
            seeded,
            {**args, "board": str(seeded[1].id)},
            decision=Decision(Effect.DENY, "operator rejected this action"),
        )
        assert denied.output == "ERROR: operator rejected this action"
        assert await _count(db, m.DataBoardBatch) == 0


def test_pep_allows_board_write_without_a_connection() -> None:
    decision = _authorize(
        _agent(),
        ToolCall(id="c1", name="board_write", arguments={}),
        frame={"tools": {}},
        tool_policies={},
        connection_key=None,
        tool_scopes=None,
    )
    assert decision.effect is Effect.ALLOW


def test_board_write_is_read_tier() -> None:
    assert classify_tier("board_write", scopes=None, config=None, annotations=None) == "read"


async def test_the_write_is_its_own_transaction(app_session: AppSessionFactory) -> None:
    """Committed on its own: the locks are free right after the call, and the
    batch survives the run's transaction rolling back."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seeded = await _seed(db, tenant)
        outcome = await _call(
            db,
            tenant,
            seeded,
            {
                "board": str(seeded[1].id),
                "dataset": "sales",
                "as_of": "2026-03-01",
                "values": {"amount": 5},
            },
        )
        assert outcome.output.startswith("Recorded 1 row"), outcome.output
        # The cap lock is not held by the still-open outer transaction.
        async with app_session(tenant) as other:
            free = await other.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"board-cap:{tenant}"},
            )
            assert free is True
        await db.rollback()
    async with app_session(tenant) as check:
        assert await _count(check, m.DataBoardBatch) == 1
        assert await _count(check, m.DataBoardRecord) == 1
