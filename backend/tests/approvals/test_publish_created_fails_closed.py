"""`approvals.publish_created` fails closed.

Whether a card is announced tenant-wide depends on its department, read under
RLS. A session whose tenant binding is gone (a caller that committed) sees no
department at all, and with the binding set to "" the read raises. Neither may
turn into a tenant-wide event carrying a personal card's title and detail, nor
crash the raiser: a lookup that fails publishes nothing (as
`realtime.emit._audience_for` does). A card filed in no department (the
tenant-wide budget incident) is still published.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from oc8 import models as m
from oc8.approvals import publish_created
from tests.approvals.test_personal_approvals import _spy, _world
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def _unbound() -> AsyncIterator[AsyncSession]:
    """A session that never set `app.tenant_id`: RLS shows it no rows."""
    engine = create_async_engine(os.environ["OC8_DATABASE_URL"], poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            yield s
    finally:
        await engine.dispose()


async def _load(app_session: AppSessionFactory, tenant: uuid.UUID, approval_id: uuid.UUID) -> Any:
    async with app_session(tenant) as db:
        return (
            await db.execute(select(m.ApprovalRequest).where(m.ApprovalRequest.id == approval_id))
        ).scalar_one()


def _created(bus: Any) -> list[str]:
    return [e[1]["approval_id"] for e in bus.tenant_events if e[0] == "approval.created"]


async def test_an_unbound_session_does_not_broadcast_a_personal_card(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(app_session)
    card = await _load(app_session, w.tenant, w.personal_card)
    bus, _channel = _spy(monkeypatch)
    async with _unbound() as db:
        await publish_created(db, card)
    assert _created(bus) == []


async def test_a_department_lookup_that_raises_publishes_nothing_and_does_not_crash(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding set to "" (what a committed transaction leaves behind):
    RLS's uuid cast raises. Even a standard card is then not published -- the
    lookup could not say it is not private."""
    w = await _world(app_session)
    card = await _load(app_session, w.tenant, w.shared_card)
    bus, _channel = _spy(monkeypatch)

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("department unreadable")

    monkeypatch.setattr("oc8.approvals.service.card_department", _boom)
    async with app_session(w.tenant) as db:
        await db.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        await publish_created(db, card)
    assert _created(bus) == []


async def test_a_card_in_no_department_is_still_published(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        card = m.ApprovalRequest(
            tenant_id=w.tenant,
            agent_id=w.nora,
            department_id=None,
            action_type="budget_incident",
            status="pending",
            title="Budget exceeded",
            detail="",
            payload={"scope": "tenant"},
        )
        db.add(card)
        await db.flush()
        card_id = card.id
    bus, _channel = _spy(monkeypatch)
    async with app_session(w.tenant) as db:
        await publish_created(db, await _load(app_session, w.tenant, card_id))
    assert _created(bus) == [str(card_id)]


async def test_a_binding_cleared_by_a_commit_neither_crashes_nor_broadcasts(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real "" binding, no stub: the caller's session must not be the one
    that reads (its uuid cast raises), and the personal card stays private."""
    w = await _world(app_session)
    card = await _load(app_session, w.tenant, w.personal_card)
    bus, _channel = _spy(monkeypatch)
    async with app_session(w.tenant) as db:
        await db.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        await publish_created(db, card)
    assert _created(bus) == []
