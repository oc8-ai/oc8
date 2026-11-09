"""GET /copilot/agent-ideas: ideas for the systems the member can see, then
generic ones, four at most; an edition may add its own."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest
from asgi_lifespan import LifespanManager
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.copilot.agent_templates import AgentTemplate
from oc8.edition.contracts import EditionExtension
from oc8.main import create_app
from tests.conftest import AppSessionFactory
from tests.copilot.builder_helpers import builder_office

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


class _Dunning(EditionExtension):
    def routers(self) -> Sequence[APIRouter]:
        return ()

    def agent_templates(self) -> Sequence[AgentTemplate]:
        return (
            AgentTemplate(
                key="dunning",
                title_en="Dunning",
                title_de="Mahnwesen",
                subtitle_en="Reminds customers who pay late",
                subtitle_de="Erinnert Kunden, die spät zahlen",
                requires_any=("odoo_mcp",),
                prompt_en="/agent Set up a dunning agent.",
                prompt_de="/agent Leg mir einen Agenten fürs Mahnwesen an.",
            ),
        )


async def _office(app_session: AppSessionFactory) -> uuid.UUID:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        other = await db.get(m.Department, office.other_department_id)
        assert other is not None
        other.frame = {"tools": {"hubspot": {"enabled": True, "read": True}}}
        # HubSpot, connected only for the department the employee does not sit in.
        db.add(
            m.McpConnection(
                tenant_id=tenant,
                department_id=office.other_department_id,
                name="hubspot",
                server_url="stdio://hubspot",
                transport="stdio",
                connected=True,
                config={"_plugin_name": "hubspot_mcp"},
            )
        )
    return tenant


async def test_ideas_follow_what_the_member_can_see(app_session: AppSessionFactory) -> None:
    tenant = await _office(app_session)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            emp = (
                await c.get(
                    "/api/v1/copilot/agent-ideas", headers=_headers(tenant, "emp", "member")
                )
            ).json()
            admin = (
                await c.get(
                    "/api/v1/copilot/agent-ideas", headers=_headers(tenant, "admin", "org_admin")
                )
            ).json()
    emp_keys = [i["key"] for i in emp]
    # Odoo is tenant-wide; HubSpot sits in a department the employee cannot see.
    assert emp_keys == ["invoice_checker", "support_triage", "weekly_sales_report", "knowledge_faq"]
    assert "lead_qualification" not in emp_keys, "requires hubspot_mcp only"
    assert emp[0]["title"]["de"] == "Rechnungsprüfer" and emp[0]["prompt"]["de"].startswith(
        "/agent "
    )
    assert [i["key"] for i in admin] == [
        "invoice_checker",
        "support_triage",
        "weekly_sales_report",
        "lead_qualification",
    ]


async def test_an_edition_adds_its_ideas(app_session: AppSessionFactory) -> None:
    tenant = await _office(app_session)
    app = create_app(edition_extensions=[_Dunning()])
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            keys = [
                i["key"]
                for i in (
                    await c.get(
                        "/api/v1/copilot/agent-ideas", headers=_headers(tenant, "emp", "member")
                    )
                ).json()
            ]
    assert "dunning" in keys


async def _keys(
    app_session: AppSessionFactory, tenant: uuid.UUID, subject: str, role: str
) -> list[str]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/copilot/agent-ideas", headers=_headers(tenant, subject, role))
    return [i["key"] for i in r.json()]


async def test_a_disconnected_system_does_not_count(app_session: AppSessionFactory) -> None:
    tenant = await _office(app_session)
    async with app_session(tenant) as db:
        for conn in (await db.execute(select(m.McpConnection))).scalars():
            conn.connected = False
    keys = await _keys(app_session, tenant, "admin", "org_admin")
    assert keys == ["knowledge_faq", "onboarding_buddy"]


async def test_a_system_in_a_department_the_member_sees_counts(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        for conn in (await db.execute(select(m.McpConnection))).scalars():
            conn.department_id = office.department_id
    assert (await _keys(app_session, tenant, "emp", "member"))[0] == "invoice_checker"


async def test_a_system_the_departments_frame_does_not_enable_does_not_count(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        books = await db.get(m.Department, office.department_id)
        assert books is not None
        books.frame = {"tools": {}}
    keys = await _keys(app_session, tenant, "emp", "member")
    assert "invoice_checker" not in keys


async def test_a_malformed_edition_template_is_refused_at_boot() -> None:
    class _Bad(EditionExtension):
        def routers(self) -> Sequence[APIRouter]:
            return ()

        def agent_templates(self) -> Sequence[AgentTemplate]:
            return (
                AgentTemplate(
                    key="bad",
                    title_en="x",
                    title_de="x",
                    subtitle_en="",
                    subtitle_de="",
                    requires_any="odoo_mcp",  # type: ignore[arg-type]
                    prompt_en="/agent x",
                    prompt_de="/agent x",
                ),
            )

    with pytest.raises(ValueError, match=r"_Bad.*'bad'"):
        create_app(edition_extensions=[_Bad()])


async def test_an_extension_that_raises_fails_startup_clearly() -> None:
    class _Boom(EditionExtension):
        def routers(self) -> Sequence[APIRouter]:
            return ()

        def agent_templates(self) -> Sequence[AgentTemplate]:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        create_app(edition_extensions=[_Boom()])


async def test_an_extension_without_the_hook_contributes_nothing() -> None:
    class _Plain:
        def routers(self) -> Sequence[APIRouter]:
            return ()

        def service_routers(self) -> Sequence[APIRouter]:
            return ()

        def tools(self) -> Sequence[object]:
            return ()

    app = create_app(edition_extensions=[_Plain()])  # type: ignore[list-item]
    assert app.state.agent_templates == ()
