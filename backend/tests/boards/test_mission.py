"""The generated mission section: built from the live spec, only for producers."""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.harness.caps import ModelCaps
from oc8.agent.preamble import build_run_preamble
from oc8.boards.mission import board_section
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

SPEC: dict[str, Any] = {
    "datasets": [
        {
            "key": "sales",
            "label": "Sales",
            "grain": "day",
            "fields": [
                {"key": "amount", "label": "Amount", "type": "money", "currency": "EUR"},
                {"key": "region", "label": "Region", "type": "category"},
            ],
        }
    ],
    "blocks": [{"type": "kpi", "dataset": "sales", "field": "amount"}],
}


async def _agent(db: AsyncSession, tenant: uuid.UUID) -> m.Agent:
    dept = m.Department(tenant_id=tenant, name="Finance", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Reporter", mission="Report.")
    db.add(agent)
    await db.flush()
    return agent


async def _board(db: AsyncSession, tenant: uuid.UUID, agent: m.Agent) -> m.DataBoard:
    board = m.DataBoard(
        tenant_id=tenant,
        department_id=agent.department_id,
        agent_id=agent.id,
        title="Revenue",
        spec=copy.deepcopy(SPEC),
    )
    db.add(board)
    await db.flush()
    return board


async def test_no_board_no_section(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        assert await board_section(db, agent=agent) is None


async def test_a_removed_board_gives_no_section(app_session: AppSessionFactory) -> None:
    import datetime as dt

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        board = await _board(db, tenant, agent)
        board.removed_at = dt.datetime.now(dt.UTC)
        await db.flush()
        assert await board_section(db, agent=agent) is None


async def test_section_names_board_datasets_fields_and_the_tool(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        board = await _board(db, tenant, agent)
        text = await board_section(db, agent=agent)
    assert text is not None
    for needle in (
        str(board.id),
        "Revenue",
        "Sales",
        "sales",
        "day",
        "amount",
        "money",
        "EUR",
        "region",
        "category",
        "board_write",
        "complete",
    ):
        assert needle in text, needle


async def test_section_reflects_the_current_spec_version(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        board = await _board(db, tenant, agent)
        before = await board_section(db, agent=agent)
        assert before is not None and "cost" not in before
        spec = copy.deepcopy(SPEC)
        spec["datasets"][0]["fields"].append({"key": "cost", "label": "Cost", "type": "number"})
        board.spec = spec
        board.spec_version = 2
        await db.flush()
        after = await board_section(db, agent=agent)
    assert after is not None and "cost" in after


async def test_preamble_sets_produces_board(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        board = await _board(db, tenant, agent)
        pre = await build_run_preamble(
            db,
            agent=agent,
            tenant_id=tenant,
            task_text="report",
            frame={},
            model_locality="eu",
            caps=ModelCaps(),
            max_steps=40,
        )
    assert pre.produces_board is True
    systems = [msg.content for msg in pre.messages if msg.role == "system"]
    assert any(isinstance(c, str) and str(board.id) in c for c in systems)


async def test_preamble_without_a_board_does_not_produce(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant)
        pre = await build_run_preamble(
            db,
            agent=agent,
            tenant_id=tenant,
            task_text="report",
            frame={},
            model_locality="eu",
            caps=ModelCaps(),
            max_steps=40,
        )
    assert pre.produces_board is False
