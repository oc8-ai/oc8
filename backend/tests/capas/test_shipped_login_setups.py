"""Every shipped tool pack whose setup has a login field only chooses the
login (unified connections design, "Storage"): no per-department row, no copied
value, no `plugin:` secret -- the pack maps the login onto its bridge itself
(`credential_env_fields`)."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from oc8 import config
from oc8 import models as m
from oc8.capas.discovery import find_plugin, invalidate_discovery_cache
from oc8.capas.manifest import parse_manifest
from oc8.connections.credential_env import credential_env_targets
from oc8.credentials.registry import get_credential_type
from oc8.credentials.service import create_credential
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, headers, install

pytestmark = pytest.mark.asyncio

_CAPAS = Path(__file__).resolve().parents[3] / "capas"

# pack -> (login field on the setup form, credential type, complete values)
PACKS = {
    "gitea_mcp": ("system", "gitea_login", {"url": "https://git.acme.test", "token": "t"}),
    "github_mcp": ("token", "github_pat", {"token": "t"}),
    "treg_mcp": ("token", "treg_token", {"token": "t"}),
}


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OC8_CAPAS_PATH", str(_CAPAS))
    config.get_settings.cache_clear()
    kek = base64.b64encode(bytes(range(32))).decode()
    monkeypatch.setattr(config.get_settings(), "secret_kek", kek, raising=False)
    invalidate_discovery_cache()
    yield
    config.get_settings.cache_clear()
    invalidate_discovery_cache()


@pytest.mark.parametrize("pack", sorted(PACKS))
async def test_a_shipped_login_setup_only_chooses_the_login(
    app_session: AppSessionFactory, pack: str
) -> None:
    field, cred_type_name, values = PACKS[pack]
    found = find_plugin(pack)
    assert found is not None and found.manifest is not None
    manifest = parse_manifest(found.manifest)
    assert manifest.tool_pack is not None
    conn_spec = manifest.tool_pack.connections[0]
    assert conn_spec.credential_type == cred_type_name and conn_spec.credential_env_fields

    tenant = uuid.uuid4()
    admin = headers(tenant, "admin@acme.test", "org_admin")
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Sales", frame={})
        db.add(dept)
        await db.flush()
        dept_id = dept.id
    async with api() as c:
        capa_id = await install(c, admin, pack)
        async with app_session(tenant) as db:
            cred = await create_credential(
                db,
                tenant_id=tenant,
                name="Company login",
                credential_type=cred_type_name,
                field_values=values,
            )
            cred_id = cred.id
        r = await c.post(
            f"/api/v1/capas/{capa_id}/setup",
            json={"values": {"department": str(dept_id), field: str(cred_id)}},
            headers=admin,
        )
        assert r.status_code == 200, r.text
        assert r.json()["credentialId"] == str(cred_id)
    async with app_session(tenant) as db:
        rows = (
            (
                await db.execute(
                    select(m.McpConnection).where(m.McpConnection.name == conn_spec.name)
                )
            )
            .scalars()
            .all()
        )
        assert [(row.department_id, row.credential_id) for row in rows] == [(None, None)]
        cfg = rows[0].config or {}
        cred_type = await get_credential_type(db, tenant_id=tenant, name=cred_type_name)
        targets = credential_env_targets(cred_type, conn_spec.credential_env_fields)
        assert not targets & (set(cfg.get("env") or {}) | set(cfg.get("secret_env") or {}))
        names = (await db.execute(select(m.Secret.name))).scalars().all()
        assert not [n for n in names if str(n).startswith("plugin:")]
        enabled = (
            (
                await db.execute(
                    select(m.CredentialDepartment.department_id).where(
                        m.CredentialDepartment.credential_id == cred_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert enabled == [dept_id]
