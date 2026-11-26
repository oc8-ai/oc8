"""`AgentDetailDTO.narrowing_tools` -- the agent's own stored narrowing,
returned verbatim alongside `effective_tools` (frame ∩ narrowing) and
`department_frame_tools`.

Exists because the Configuration and Guardrails tabs both resave whatever
fields they don't themselves edit by reading from *some* prior source --
and until this field existed, that source was `effective_tools`, which used
to dip to zero whenever `agent.role_id` dangled or resolved to no rights
(the role term the permission algebra has since dropped entirely -- see
`authz.pdp`'s module docstring; `Agent.role_id` stays in the schema,
dormant, per this redesign's own constraint, so it can no longer degrade
anything). `narrowing_tools` gives the frontend a source immune to that
kind of dip regardless.
"""

from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def _seed_agent_with_narrowing(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, role_id: uuid.UUID | None = None
) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant,
            name="General",
            frame={"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}},
        )
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Lennart",
            role_id=role_id,
            narrowing={
                "tools": {
                    "odoo": {
                        "enabled": True,
                        "read": True,
                        "modify": True,
                        "connection_id": None,
                    }
                }
            },
        )
        db.add(agent)
        await db.flush()
        return agent.id


async def test_narrowing_tools_reflects_the_agents_own_stored_narrowing(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent_with_narrowing(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(f"/api/v1/agents/{agent_id}", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["narrowingTools"]["odoo"] == {
                "enabled": True,
                "read": True,
                "modify": True,
                "approvalEur": None,
                "approvalActions": [],
                "only": None,
                "connectionId": None,
                "conditions": [],
                "limits": [],
            }


async def test_a_dangling_role_id_no_longer_touches_effective_tools(
    app_session: AppSessionFactory,
) -> None:
    """The regression this field was built to work around no longer exists:
    `effective_tools` used to dip to zero when `agent.role_id` dangled, because
    the old permission algebra intersected in `role_rights`. That term is gone
    (`authz.pdp.effective_tool_policies` takes only frame and narrowing now),
    so a dangling role_id -- which the schema still allows, dormant -- must
    have no effect on `effectiveTools`, `narrowingTools`, or
    `departmentFrameTools` at all; all three should read exactly as if
    `role_id` were unset."""
    tenant = uuid.uuid4()
    dangling_role_id = uuid.uuid4()
    agent_id = await _seed_agent_with_narrowing(app_session, tenant, role_id=dangling_role_id)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(f"/api/v1/agents/{agent_id}", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            body = r.json()

            assert body["effectiveTools"]["odoo"]["read"] is True
            assert body["effectiveTools"]["odoo"]["modify"] is True
            assert body["narrowingTools"]["odoo"]["read"] is True
            assert body["narrowingTools"]["odoo"]["modify"] is True
            assert body["departmentFrameTools"]["odoo"]["read"] is True
            assert body["departmentFrameTools"]["odoo"]["modify"] is True


async def test_narrowing_overridden_keys_is_exposed_and_distinct_from_narrowing_tools_presence(
    app_session: AppSessionFactory,
) -> None:
    """Regression, found via live verification: the agent detail page's
    status badge used to key off mere presence in `narrowingTools` (every
    save rewrites every currently-relevant key, touched or not), so every
    agent with ANY saved narrowing showed "Narrowed" on every tool. The DTO
    must expose the real, explicit signal (`Agent.narrowing_overridden_keys`,
    the ONLY field `set_narrowing` ever writes to) so the frontend can tell
    "has a narrowing entry" apart from "deliberately diverged"."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant,
            name="General",
            frame={"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}},
        )
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Lennart",
            # A narrowing entry for "odoo" exists (as every save writes one
            # for every currently-relevant key), but the agent never
            # deliberately overrode it -- narrowing_overridden_keys stays empty.
            narrowing={"tools": {"odoo": {"enabled": True, "read": True, "modify": True}}},
            narrowing_overridden_keys=[],
        )
        db.add(agent)
        await db.flush()
        agent_id = agent.id
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(f"/api/v1/agents/{agent_id}", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            body = r.json()
            assert "odoo" in body["narrowingTools"]
            assert body["narrowingOverriddenKeys"] == []


async def test_narrowing_tools_is_empty_for_an_agent_with_no_narrowing_set(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="General", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Fresh")
        db.add(agent)
        await db.flush()
        agent_id = agent.id
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(f"/api/v1/agents/{agent_id}", headers=_headers(tenant))
            assert r.status_code == 200, r.text
            assert r.json()["narrowingTools"] == {}
