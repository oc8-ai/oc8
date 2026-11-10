"""`GET /copilot/proposals` -- what the Copilot dock reads to show a human
what the Assistant has proposed.

Without a listing there was no live UI for a proposal at all: `propose_change`
answers the model with a sentence naming the id, the dock never parsed it, and
the apply/reject mutations had zero call sites. Listing by STATUS rather than
correlating to a chat message is also what makes a proposal raised over
Telegram reviewable on the web.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import Principal, get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="boss", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _draft_a_mission_change(app_session: AppSessionFactory, tenant: uuid.UUID) -> str:
    """One real proposal, made the way the Assistant makes them -- through
    `create_proposal`, which is the only writer and only ever writes 'draft'."""
    from oc8.copilot.proposals import create_proposal

    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora", mission="old")
        db.add(agent)
        await db.flush()
        proposal = await create_proposal(
            db,
            Principal(subject="assistant", tenant_id=tenant, role="agent", kind="agent"),
            [{"type": "agent.mission.set", "agentId": str(agent.id), "mission": "new"}],
        )
        return str(proposal.id)


async def test_a_drafted_proposal_is_listed(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    proposal_id = await _draft_a_mission_change(app_session, tenant)
    async with _http() as http:
        r = await http.get("/api/v1/copilot/proposals", headers=_headers(tenant))
        assert r.status_code == 200, r.text
        listed = r.json()
        assert [p["id"] for p in listed] == [proposal_id]
        assert listed[0]["status"] == "draft"
        # The same label/reference shape the detail route returns -- the dock
        # renders one component for both.
        assert listed[0]["operations"][0]["label"] == "agent.mission.set"


async def test_an_applied_proposal_stops_being_listed(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    proposal_id = await _draft_a_mission_change(app_session, tenant)
    async with _http() as http:
        applied = await http.post(
            f"/api/v1/copilot/proposals/{proposal_id}/apply", headers=_headers(tenant)
        )
        assert applied.status_code == 200, applied.text
        r = await http.get("/api/v1/copilot/proposals", headers=_headers(tenant))
        assert r.json() == []


async def test_a_rejected_proposal_stops_being_listed(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    proposal_id = await _draft_a_mission_change(app_session, tenant)
    async with _http() as http:
        rejected = await http.post(
            f"/api/v1/copilot/proposals/{proposal_id}/reject", headers=_headers(tenant)
        )
        assert rejected.status_code == 200, rejected.text
        r = await http.get("/api/v1/copilot/proposals", headers=_headers(tenant))
        assert r.json() == []


async def test_one_tenants_proposals_are_not_listed_in_another(
    app_session: AppSessionFactory,
) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    await _draft_a_mission_change(app_session, tenant_a)
    async with _http() as http:
        r = await http.get("/api/v1/copilot/proposals", headers=_headers(tenant_b))
        assert r.status_code == 200, r.text
        assert r.json() == []


async def _draft_a_builder_proposal(app_session: AppSessionFactory, tenant: uuid.UUID) -> str:
    from tests.copilot.builder_helpers import builder_office, propose_as_admin

    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        proposal = await propose_as_admin(db, office)
        return str(proposal.id)


async def test_a_builder_proposal_is_not_listed(app_session: AppSessionFactory) -> None:
    """Ruling A10: a builder proposal is its owner's chat content, read only
    through its own route -- never in the generic listing."""
    tenant = uuid.uuid4()
    proposal_id = await _draft_a_mission_change(app_session, tenant)
    await _draft_a_builder_proposal(app_session, tenant)
    async with _http() as http:
        r = await http.get("/api/v1/copilot/proposals", headers=_headers(tenant))
        assert r.status_code == 200, r.text
        assert [p["id"] for p in r.json()] == [proposal_id]


async def test_a_builder_proposal_cannot_be_reviewed_generically(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    builder_id = await _draft_a_builder_proposal(app_session, tenant)
    async with _http() as http:
        r = await http.get(f"/api/v1/copilot/proposals/{builder_id}", headers=_headers(tenant))
        assert r.status_code == 404, r.text
