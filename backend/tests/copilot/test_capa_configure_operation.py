"""capa.configure -- scoped to capas with no MCP connection and no
password/credential setup fields; secrets never flow through this path."""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.auth import Principal
from oc8.copilot.proposals import ProposalRejected, apply_proposal, create_proposal

pytestmark = pytest.mark.asyncio


def _actor(tenant: uuid.UUID) -> Principal:
    return Principal(subject="operator-1", tenant_id=tenant, role="org_admin")


async def _installed_capa(db, tenant, *, manifest: dict) -> m.Capa:
    capa = m.Capa(
        tenant_id=tenant,
        name=manifest["name"],
        type="approval_channel",
        trust_level="first_party",
    )
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
        m.CapaInstallation(
            tenant_id=tenant, capa_id=capa.id, status="enabled", granted_permissions=[]
        )
    )
    await db.flush()
    return capa


_PLAIN_MANIFEST = {
    "name": "webhook_notifier",
    "version": "1.0.0",
    "type": "approval_channel",
    "setup": {
        "title": "Webhook notifier setup",
        "fields": [
            {
                "key": "channel_name",
                "label": "Channel name",
                "kind": "text",
                "required": True,
                "default": "",
            }
        ],
    },
}

_SECRET_MANIFEST = {
    "name": "secure_notifier",
    "version": "1.0.0",
    "type": "approval_channel",
    "setup": {
        "title": "Secure notifier setup",
        "fields": [
            {
                "key": "api_key",
                "label": "API key",
                "kind": "password",
                "required": True,
                "default": "",
            }
        ],
    },
}


async def test_configure_applies_plain_fields(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        capa = await _installed_capa(db, acme_tenant, manifest=_PLAIN_MANIFEST)
        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "capa.configure",
                    "capaId": str(capa.id),
                    "values": {"channel_name": "#ops"},
                }
            ],
        )
        result = await apply_proposal(db, proposal.id, actor)
        assert result.status == "applied"


async def test_configure_rejects_a_password_kind_field_outright(app_session, acme_tenant) -> None:
    actor = _actor(acme_tenant)
    async with app_session(acme_tenant) as db:
        capa = await _installed_capa(db, acme_tenant, manifest=_SECRET_MANIFEST)
        proposal = await create_proposal(
            db,
            actor,
            [
                {
                    "type": "capa.configure",
                    "capaId": str(capa.id),
                    "values": {"api_key": "sk-should-be-rejected"},
                }
            ],
        )
        with pytest.raises(ProposalRejected):
            await apply_proposal(db, proposal.id, actor)
