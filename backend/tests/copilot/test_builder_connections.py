"""The builder offers what the runtime can run (connections design): an
enabled integration with usable access counts as a connected system, even
without a connected McpConnection row or a frame entry -- one rule shared with
`connections/runtime_env.integration_connections`."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.capas.discovery import find_plugin
from oc8.config import get_settings
from oc8.copilot.agent_templates import connected_kinds
from oc8.copilot.builder import usable_systems
from oc8.copilot.models import CopilotProposal
from tests.conftest import AppSessionFactory
from tests.copilot.builder_helpers import BuilderOffice, builder_office
from tests.copilot.test_builder_tools import _call, _run, _stored_steps

pytestmark = pytest.mark.asyncio

_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"


@pytest.fixture(autouse=True)
def _plugins_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OC8_CAPAS_PATH", str(_PLUGINS_DIR))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _install(
    db: AsyncSession, tenant: uuid.UUID, plugin: str, *, status: str = "enabled"
) -> None:
    """The plugin installed from disk (rows only) plus the department-less,
    never-connected template row enabling it writes."""
    found = find_plugin(plugin)
    assert found is not None and found.manifest is not None
    manifest: dict[str, Any] = found.manifest
    capa = m.Capa(tenant_id=tenant, name=plugin, type="tool_pack", trust_level="first_party")
    db.add(capa)
    await db.flush()
    version = m.CapaVersion(
        tenant_id=tenant,
        capa_id=capa.id,
        semver="1.0.0",
        manifest=manifest,
        artifact_hash=b"",
        permissions=[],
        capabilities=[],
        entry_points={},
    )
    db.add(version)
    await db.flush()
    capa.current_version_id = version.id
    db.add(
        m.CapaInstallation(tenant_id=tenant, capa_id=capa.id, status=status, granted_permissions=[])
    )
    tool_key = {"odoo_mcp": "odoo", "microsoft365": "microsoft365"}[plugin]
    db.add(
        m.McpConnection(
            tenant_id=tenant,
            name=tool_key,
            server_url=f"stdio://{tool_key}",
            transport="stdio",
            connected=False,
            config={"_plugin_name": plugin, "_connection_key": "primary"},
        )
    )
    await db.flush()


async def _org_login(db: AsyncSession, tenant: uuid.UUID, name: str = "Odoo") -> None:
    db.add(
        m.Credential(
            tenant_id=tenant,
            name=name,
            credential_type="odoo_login",
            field_values={"url": "https://odoo.acme.test", "username": "bot"},
            secret_refs={},
            owner_member_id=None,
        )
    )
    await db.flush()


async def _office_without_legacy(db: AsyncSession, tenant: uuid.UUID) -> BuilderOffice:
    """The builder office minus its connected legacy `odoo` row."""
    office = await builder_office(db, tenant)
    legacy = (
        await db.execute(select(m.McpConnection).where(m.McpConnection.tenant_id == tenant))
    ).scalar_one()
    await db.delete(legacy)
    await db.flush()
    return office


async def _department(db: AsyncSession, office: BuilderOffice) -> m.Department:
    dept = await db.get(m.Department, office.other_department_id)
    assert dept is not None
    return dept


async def test_an_organisation_odoo_login_and_a_microsoft_sign_in_are_usable(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office_without_legacy(db, tenant)
        await _install(db, tenant, "odoo_mcp")
        await _install(db, tenant, "microsoft365")
        await _org_login(db, tenant)
        # "Entwicklung" lists no tool at all: an agent-exclusive grant.
        dept = await _department(db, office)
        assert await usable_systems(db, tenant_id=tenant, department=dept) == {
            "odoo",
            "microsoft365",
        }
        kinds = await connected_kinds(db, tenant_id=tenant, departments=[dept])
        assert kinds == {"odoo_mcp", "microsoft365"}


async def test_a_department_that_switched_a_tool_off_does_not_get_it(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office_without_legacy(db, tenant)
        await _install(db, tenant, "odoo_mcp")
        await _install(db, tenant, "microsoft365")
        await _org_login(db, tenant)
        dept = await _department(db, office)
        dept.frame = {"tools": {"microsoft365": {"enabled": False, "read": True}}}
        await db.flush()
        assert await usable_systems(db, tenant_id=tenant, department=dept) == {"odoo"}
        assert await connected_kinds(db, tenant_id=tenant, departments=[dept]) == {"odoo_mcp"}


@pytest.mark.parametrize("logins", [0, 2])
async def test_a_login_type_needs_exactly_one_organisation_login(
    app_session: AppSessionFactory, logins: int
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office_without_legacy(db, tenant)
        await _install(db, tenant, "odoo_mcp")
        for i in range(logins):
            await _org_login(db, tenant, name=f"Odoo {i}")
        anna = m.OrgMember(
            tenant_id=tenant,
            subject="anna",
            subject_uuid=uuid.uuid4(),
            display_name="Anna",
            email="anna@acme.test",
        )
        db.add(anna)
        await db.flush()
        # A person's own login does not make it usable for a new agent.
        db.add(
            m.Credential(
                tenant_id=tenant,
                name="Mine",
                credential_type="odoo_login",
                field_values={},
                secret_refs={},
                owner_member_id=anna.id,
            )
        )
        await db.flush()
        dept = await _department(db, office)
        assert await usable_systems(db, tenant_id=tenant, department=dept) == frozenset()
        assert await connected_kinds(db, tenant_id=tenant, departments=[dept]) == frozenset()


async def test_a_disabled_plugin_is_not_usable(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office_without_legacy(db, tenant)
        await _install(db, tenant, "odoo_mcp", status="installed")
        await _org_login(db, tenant)
        dept = await _department(db, office)
        assert await usable_systems(db, tenant_id=tenant, department=dept) == frozenset()


async def test_builder_options_say_why_a_system_is_missing(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office_without_legacy(db, tenant)
        await _install(db, tenant, "odoo_mcp")
        run, task = await _run(db, office, chat_mode="agent")
        outcome = await _call(db, office, run, task, "builder_options")
        assert outcome is not None and not outcome.output.startswith("ERROR")
        line = next(ln for ln in outcome.output.splitlines() if "Entwicklung" in ln)
        assert "| none |" in line or line.rstrip().endswith("| none"), line
        assert "no organisation login yet" in line and "/connections" in line, line
        assert "/knowledge" not in outcome.output


async def test_a_proposal_may_use_an_organisation_login_without_a_frame_entry(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office_without_legacy(db, tenant)
        await _install(db, tenant, "odoo_mcp")
        await _org_login(db, tenant)
        run, task = await _run(db, office, chat_mode="agent")
        outcome = await _call(
            db,
            office,
            run,
            task,
            "propose_agent",
            departmentId=str(office.other_department_id),
            name="Rechnungsprüfer",
            roleTitle="Buchhaltung",
            mission="Prüft offene Rechnungen.",
            modelId=str(office.model_id),
            systems=["odoo"],
        )
        assert not outcome.output.startswith("ERROR"), outcome.output
        narrowing = (await _stored_steps(db))[-1]["narrowing"]
        # No frame entry: read on, writes stay off (agent-exclusive grant).
        assert narrowing == {"tools": {"odoo": {"enabled": True, "read": True, "modify": False}}}
        assert (await db.execute(select(CopilotProposal))).scalars().all()


async def test_builder_options_text_lists_an_organisation_login(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office_without_legacy(db, tenant)
        await _install(db, tenant, "odoo_mcp")
        await _org_login(db, tenant)
        run, task = await _run(db, office, chat_mode="agent")
        outcome = await _call(db, office, run, task, "builder_options")
        line = next(ln for ln in outcome.output.splitlines() if "Entwicklung" in ln)
        assert "| odoo" in line, line
