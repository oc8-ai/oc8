"""capa.install / capa.disable."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import Principal
from oc8.copilot.proposals import ProposalRejected, apply_proposal, create_proposal

pytestmark = pytest.mark.asyncio


def _actor(tenant: uuid.UUID) -> Principal:
    return Principal(subject="operator-1", tenant_id=tenant, role="org_admin")


async def test_capa_install_rejects_a_plugin_id_not_found_on_disk(
    app_session, acme_tenant
) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        proposal = await create_proposal(
            db, actor, [{"type": "capa.install", "diskPluginId": "does-not-exist-on-disk"}]
        )
        with pytest.raises(ProposalRejected):
            await apply_proposal(db, proposal.id, actor)


async def test_capa_disable_rejects_an_unknown_capa(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        proposal = await create_proposal(
            db, actor, [{"type": "capa.disable", "capaId": str(uuid.uuid4())}]
        )
        with pytest.raises(ProposalRejected):
            await apply_proposal(db, proposal.id, actor)


async def test_capa_disable_disables_an_enabled_capa(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        capa = m.Capa(
            tenant_id=acme_tenant, name="github_mcp", type="tool_pack", trust_level="first_party"
        )
        db.add(capa)
        await db.flush()
        db.add(
            m.CapaInstallation(
                tenant_id=acme_tenant, capa_id=capa.id, status="enabled", granted_permissions=[]
            )
        )
        await db.flush()

        proposal = await create_proposal(
            db, actor, [{"type": "capa.disable", "capaId": str(capa.id)}]
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"

        installation = (
            await db.execute(
                select(m.CapaInstallation).where(m.CapaInstallation.capa_id == capa.id)
            )
        ).scalar_one()
        assert installation.status == "disabled"
