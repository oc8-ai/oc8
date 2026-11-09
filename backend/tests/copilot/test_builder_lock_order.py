"""Lock order between a run's `propose_agent` and the card's routes.

The one-way rule: the tenant audit lock first, everything else after. A run
holds the audit lock from its `tool.call:propose_agent` event on; the card's
"Anlegen" / "Zur Freigabe senden" and the approvals funnel lock the proposal
first and append audit after. So `create_builder_proposal` must never WAIT on a
builder row: a row somebody else holds is a concurrent change, answered with
`superseded_concurrently` (the model retries), never a deadlock.

The run side takes the tenant lock explicitly, exactly as `append_event` takes
it, so these hold whether the audit chain locks at append or at commit."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.auth import Principal
from oc8.copilot.builder import BUILDER_KIND, apply_builder_proposal, lock_builder_proposal
from oc8.copilot.builder_ops import BuilderInvalid
from oc8.copilot.models import CopilotProposal
from tests.conftest import AppSessionFactory
from tests.copilot.builder_helpers import (
    BuilderOffice,
    builder_office,
    invoice_checker_ops,
    propose_as_admin,
)

pytestmark = pytest.mark.asyncio


async def _take_tenant_audit_lock(db: AsyncSession, tenant: uuid.UUID) -> None:
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:tenant, 0))"),
        {"tenant": str(tenant)},
    )


async def _waiting_on_the_audit_lock(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    """Until some transaction queues behind an advisory lock (the route's audit
    append, after it locked the proposal)."""
    for _ in range(100):
        async with app_session(tenant) as probe:
            waiting = await probe.scalar(
                text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
            )
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the route never queued behind the audit lock")


async def _propose_like_the_tool(
    db: AsyncSession, office: BuilderOffice, name: str
) -> CopilotProposal:
    """In a savepoint, as `propose_agent` calls it; bounded, so a wait that is
    not a deadlock still fails the test instead of hanging it."""
    async with db.begin_nested():
        return await asyncio.wait_for(
            propose_as_admin(db, office, invoice_checker_ops(office, name=name)), timeout=10
        )


async def _drafts(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[CopilotProposal]:
    async with app_session(tenant) as db:
        return list(
            (
                await db.execute(
                    select(CopilotProposal).where(
                        CopilotProposal.kind == BUILDER_KIND, CopilotProposal.status == "draft"
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_proposing_while_the_card_applies_does_not_deadlock(
    app_session: AppSessionFactory,
) -> None:
    """A new propose_agent in the run ("Anpassen") and "Anlegen" at once."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        draft = await propose_as_admin(db, office)
    draft_id = draft.id

    async def anlegen() -> uuid.UUID:
        async with app_session(tenant) as db:
            return await apply_builder_proposal(
                db,
                proposal_id=draft_id,
                expect_status="draft",
                applied_by="admin",
                principal=Principal(subject="admin", tenant_id=tenant, role="org_admin"),
                now=dt.datetime.now(tz=dt.UTC),
            )

    refusal: BuilderInvalid | None = None
    async with app_session(tenant) as run:
        await _take_tenant_audit_lock(run, tenant)  # the run's tool.call event
        route = asyncio.create_task(anlegen())
        await _waiting_on_the_audit_lock(app_session, tenant)
        try:
            await _propose_like_the_tool(run, office, "Rechnungsprüferin")
        except BuilderInvalid as exc:
            refusal = exc
    assert refusal is not None and refusal.code == "superseded_concurrently"
    agent_id = await asyncio.wait_for(route, timeout=10)
    async with app_session(tenant) as db:
        applied = await db.get(CopilotProposal, draft_id)
        assert applied is not None and applied.status == "applied"
        assert await db.get(m.Agent, agent_id) is not None
    assert await _drafts(app_session, tenant) == []


async def test_proposing_while_an_expired_request_is_held_does_not_deadlock(
    app_session: AppSessionFactory,
) -> None:
    """The expired-awaiting branch: the approvals funnel holds the request's
    proposal and queues for audit; the run may not wait on that row either."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        waiting = await propose_as_admin(db, office)
        waiting.status = "awaiting_approval"
        waiting.expires_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=1)
    waiting_id = waiting.id

    async def funnel() -> None:
        async with app_session(tenant) as db:
            assert await lock_builder_proposal(db, waiting_id) is not None
            await _take_tenant_audit_lock(db, tenant)

    refusal: BuilderInvalid | None = None
    async with app_session(tenant) as run:
        await _take_tenant_audit_lock(run, tenant)
        holder = asyncio.create_task(funnel())
        await _waiting_on_the_audit_lock(app_session, tenant)
        try:
            await _propose_like_the_tool(run, office, "Rechnungsprüferin")
        except BuilderInvalid as exc:
            refusal = exc
    assert refusal is not None and refusal.code == "superseded_concurrently"
    await asyncio.wait_for(holder, timeout=10)
    # Once the holder is gone, the retry goes through.
    async with app_session(tenant) as run:
        retried = await _propose_like_the_tool(run, office, "Rechnungsprüferin")
        assert retried.status == "draft"
    async with app_session(tenant) as db:
        expired = await db.get(CopilotProposal, waiting_id)
        assert expired is not None and expired.status == "expired"
