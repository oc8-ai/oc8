# backend/tests/agents/test_assistant_provisioning.py
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_first_call_creates_a_tenant_wide_team_lead(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await get_or_create_assistant(db, tenant_id=tenant)
        assert agent.is_team_lead is True
        assert agent.is_tenant_assistant is True
        dept = await db.get(m.Department, agent.department_id)
        assert dept is not None
        assert dept.tenant_id == tenant
        assert dept.team_lead_agent_id == agent.id


async def test_second_call_returns_the_same_row(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        first = await get_or_create_assistant(db, tenant_id=tenant)
        second = await get_or_create_assistant(db, tenant_id=tenant)
        assert first.id == second.id


async def test_each_tenant_gets_its_own_assistant(app_session: AppSessionFactory) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant_a) as db:
        assistant_a = await get_or_create_assistant(db, tenant_id=tenant_a)
    async with app_session(tenant_b) as db:
        assistant_b = await get_or_create_assistant(db, tenant_id=tenant_b)
    assert assistant_a.id != assistant_b.id
    assert assistant_a.department_id != assistant_b.department_id


async def test_two_concurrent_first_calls_agree_on_one_assistant(
    app_session: AppSessionFactory,
) -> None:
    """Two tabs, two transactions, a tenant that has never had an Assistant.

    There are five concurrent first-call entrypoints for
    `get_or_create_assistant` (GET /assistant, the three /chat/sessions...
    routes via `_assistant_visible`, and Telegram's `bind_from_free_text`), and
    until migration 0082 this was a bare check-then-insert: both callers saw
    nothing, both created an Assistant AND a department, and every later lookup
    answered with whichever row the planner returned first.

    Shaped after `tests/authz/test_scope_resolution.py`'s own two-first-requests
    test: the second transaction is made to arrive while the first is still
    open, which is the only ordering that exercises the conflict at all.
    """
    tenant = uuid.uuid4()
    first_is_in = asyncio.Event()

    async def _first() -> uuid.UUID:
        async with app_session(tenant) as db:
            agent = await get_or_create_assistant(db, tenant_id=tenant)
            first_is_in.set()
            # Still uncommitted while the second call runs into it.
            await asyncio.sleep(0.2)
            return agent.id

    async def _second() -> uuid.UUID:
        await first_is_in.wait()
        async with app_session(tenant) as db:
            agent = await get_or_create_assistant(db, tenant_id=tenant)
            return agent.id

    one, two = await asyncio.wait_for(asyncio.gather(_first(), _second()), timeout=30)
    assert one == two, "two tabs provisioned two Assistants for one tenant"

    async with app_session(tenant) as db:
        agents = (
            (
                await db.execute(
                    select(m.Agent).where(
                        m.Agent.tenant_id == tenant,
                        m.Agent.is_tenant_assistant.is_(True),
                        m.Agent.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(agents) == 1
        # The loser's half-built Department went down with its savepoint.
        departments = (
            (
                await db.execute(
                    select(m.Department).where(
                        m.Department.tenant_id == tenant,
                        m.Department.is_assistant_department.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(departments) == 1


async def _model_config(db: AsyncSession, tenant: uuid.UUID, *, copilot: bool) -> m.ModelConfig:
    row = m.ModelConfig(
        tenant_id=tenant,
        provider="anthropic",
        model="claude-sonnet-4",
        used_by_copilot=copilot,
    )
    db.add(row)
    await db.flush()
    return row


async def test_the_assistant_follows_the_used_by_copilot_flag_after_creation(
    app_session: AppSessionFactory,
) -> None:
    """Settings -> Models moving the `used_by_copilot` flag must move the
    Assistant's model too.

    `_select_model_config` only ever ran on the creation path, and
    `get_or_create_assistant`'s early return skipped it for every later call --
    so an Assistant stayed pinned to whatever was flagged the moment it was
    first lazily provisioned, for ever, with no UI anywhere that could change
    it.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        first = await _model_config(db, tenant, copilot=True)
        second = await _model_config(db, tenant, copilot=False)
        first_id, second_id = first.id, second.id

        agent = await get_or_create_assistant(db, tenant_id=tenant)
        assert agent.model_config_id == first_id

        # The operator re-flags the other config in Settings -> Models.
        first.used_by_copilot = False
        second.used_by_copilot = True
        await db.flush()

        again = await get_or_create_assistant(db, tenant_id=tenant)
        assert again.id == agent.id, "still the same Assistant, not a new one"
        assert again.model_config_id == second_id


async def test_an_existing_assistant_keeps_its_model_when_nothing_is_configured(
    app_session: AppSessionFactory,
) -> None:
    """A tenant with no usable ModelConfig at all must not have its Assistant's
    working pin cleared by the re-resolve -- `None` there means "no answer
    right now", not "unset it"."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cfg = await _model_config(db, tenant, copilot=True)
        cfg_id = cfg.id
        agent = await get_or_create_assistant(db, tenant_id=tenant)
        assert agent.model_config_id == cfg_id

        # Blank out the provider/model, which is what `_select_model_config`
        # filters on -- there is now no selectable config in the tenant.
        cfg.provider = ""
        cfg.model = ""
        await db.flush()

        again = await get_or_create_assistant(db, tenant_id=tenant)
        assert again.model_config_id == cfg_id


async def test_existing_assistant_gets_the_current_mission(
    app_session: AppSessionFactory,
) -> None:
    from oc8.agent.assistant import _MISSION

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await get_or_create_assistant(db, tenant_id=tenant)
        agent.mission = "old mission"
        await db.flush()
        again = await get_or_create_assistant(db, tenant_id=tenant)
        assert again.mission == _MISSION
        assert "responsibility_open" in _MISSION and "schedule_followup" in _MISSION
