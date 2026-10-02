"""The gate, not the notification.

Spec §2.8 rules that publish hooks are synchronous, run inside the publish
transaction, are ordered by registration, and that a hook which raises FAILS
the publish. Every one of those four is asserted here, because each of them is
something an "improvement" to this module would plausibly remove: making the
hooks async, moving them outside the transaction, making the order a set, or
swallowing an exception so a publish "at least goes through".
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.publish_hooks import (
    PublishHookFailed,
    _reset_publish_hooks_for_tests,
    register_publish_hook,
    registered_publish_hooks,
    run_publish_hooks,
)
from oc8.agents.versioning import publish_version
from oc8.constants import ACME_TENANT_ID
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _clean_registry() -> AsyncIterator[None]:
    """The registry is a module global. Without this every test in the suite
    that publishes an agent version would inherit whatever a hook test
    registered, and the failure would surface somewhere else entirely."""
    _reset_publish_hooks_for_tests()
    yield
    _reset_publish_hooks_for_tests()


async def _make_agent(db: AsyncSession, tenant: uuid.UUID) -> m.Agent:
    department = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex}", frame={})
    db.add(department)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=department.id,
        name=f"A-{uuid.uuid4().hex[:8]}",
        mission="original",
    )
    db.add(agent)
    await db.flush()
    return agent


def test_core_registers_no_publish_hooks() -> None:
    """The seam stays domain-neutral. A hook shipped in core would give the
    registry an opinion about compliance or evaluation before either subsystem
    exists, and would make every publish in every test depend on it."""
    assert registered_publish_hooks() == ()


async def test_hooks_run_in_registration_order(app_session: AppSessionFactory) -> None:
    calls: list[str] = []

    async def first(_db: AsyncSession, _version: m.AgentVersion) -> None:
        calls.append("first")

    async def second(_db: AsyncSession, _version: m.AgentVersion) -> None:
        calls.append("second")

    register_publish_hook("first", first)
    register_publish_hook("second", second)

    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        version = m.AgentVersion(
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            version_no=1,
            payload={},
            payload_hash=b"\x00" * 32,
        )
        await run_publish_hooks(db, version)

    assert calls == ["first", "second"]


def test_a_duplicate_hook_name_is_refused() -> None:
    """Two registrations of the same name is a double import, and a compliance
    gate that runs twice is a gate whose second verdict nobody reasoned about."""

    async def hook(_db: AsyncSession, _version: m.AgentVersion) -> None:
        return None

    register_publish_hook("only", hook)
    with pytest.raises(ValueError, match="already registered"):
        register_publish_hook("only", hook)


async def test_a_raising_hook_fails_the_publish_and_no_version_lands(
    app_session: AppSessionFactory,
) -> None:
    """The whole reason these are synchronous and in-transaction.

    A hook that refuses must leave the agent exactly as it was -- no new
    version row, `current_version_id` unmoved. An asynchronous notification
    could not do this, which is why spec §2.8 refuses Redis pub/sub for it.
    """

    async def refuse(_db: AsyncSession, _version: m.AgentVersion) -> None:
        raise RuntimeError("policy says no")

    tenant = uuid.UUID(str(ACME_TENANT_ID))
    agent_id: uuid.UUID
    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        agent_id = agent.id
        before = await publish_version(db, agent, note="v1")
        assert before.version_no == 1

    # Registered only now: the hook must block v2, not the v1 publish that
    # already happened above (there is nothing left to veto retroactively).
    register_publish_hook("refuse", refuse)

    async with app_session(tenant) as db:
        reloaded = await db.get(m.Agent, agent_id)
        assert reloaded is not None
        reloaded.mission = "changed"
        with pytest.raises(PublishHookFailed) as caught:
            await publish_version(db, reloaded, note="v2")
        assert caught.value.hook_name == "refuse"
        assert "policy says no" in str(caught.value)
        await db.rollback()

    async with app_session(tenant) as db:
        total = (
            await db.execute(
                select(func.count())
                .select_from(m.AgentVersion)
                .where(m.AgentVersion.agent_id == agent_id)
            )
        ).scalar_one()
        # Scoped to THIS agent: ACME_TENANT_ID is shared across tests with no
        # per-test rollback, so a tenant-wide count would be meaningless.
        assert total == 1
        reloaded = await db.get(m.Agent, agent_id)
        assert reloaded is not None
        assert reloaded.mission == "original"
